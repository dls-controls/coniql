# Treat all division as float division even in python2
from __future__ import division

from collections import Counter
from dataclasses import dataclass
from typing import Dict

import numpy as np
from annotypes import TYPE_CHECKING, Sequence
from scanpointgenerator import Point, CompoundGenerator, StaticPointGenerator

# from malcolm.core import Context
# from malcolm.modules import builtin, scanning
# from .infos import MotorInfo
from device.channel.multi import get_all
from device.devices.motor import MotorCs, PmacMotor
from device.devices.pmac import CsAxisMapping, PmacMotors
from device.pmacutil.pmacenums import UserPrograms, PointType
from device.pmacutil.pmacconst import CS_AXIS_NAMES, MIN_TIME, MIN_INTERVAL, \
    CsAxis
from device.pmacutil.scanningutil import MotionTrigger
from device.pmacutil.velocityprofile import VelocityProfile
from device.util import asyncio_gather_values

if TYPE_CHECKING:
    from typing import Dict, Set, List

    Profiles = Dict[str, List[float]]


@dataclass
class MotorInfo:
    cs: MotorCs
    acceleration: float
    resolution: float
    offset: float
    max_velocity: float
    current_position: float
    scannable: str
    velocity_settle: float
    units: str

    def acceleration_time(self, v1, v2):
        # The time taken to ramp from v1 to pad_velocity
        ramp_time = abs(v2 - v1) / self.acceleration
        return ramp_time

    def ramp_distance(self, v1, v2, ramp_time=None):
        # The distance moved in the first part of the ramp
        if ramp_time is None:
            ramp_time = self.acceleration_time(v1, v2)
        ramp_distance = (v1 + v2) * ramp_time / 2
        return ramp_distance

    def make_velocity_profile(
            self, v1, v2, distance, min_time, min_interval=0.002):
        """Calculate PVT points that will perform the move within motor params
        Args:
            v1 (float): Starting velocity in EGUs/s
            v2 (float): Ending velocity in EGUs/s
            distance (float): Relative distance to travel in EGUs
            min_time (float): The minimum time the move should take
            min_interval (float): Minimum time between profile points
        Returns:
            VelocityProfile: defining a list of times and velocities
        """

        # Create the time and velocity arrays
        p = VelocityProfile(
            v1, v2, distance, min_time, self.acceleration, self.max_velocity,
            self.velocity_settle, min_interval)
        p.get_profile()
        return p

    def in_cts(self, position):
        # type: (float) -> int
        """Return the position (in EGUs) translated to counts"""
        cts = int(round((position - self.offset) / self.resolution))
        return cts


async def cs_port_with_motors_in(motors: PmacMotors) -> str:
    for motor in motors.iterator():
        cs = await motor.cs()
        if cs.axis in CS_AXIS_NAMES:
            return cs.port
    raise ValueError("Can't find a cs port to use in %s" % layout_table.name)


def get_motion_axes(generator, axes_to_move):
    # type: (CompoundGenerator, Sequence[str]) -> List[str]
    """Filter axes_to_move to only contain motion axes"""
    static_generator_axes = set()
    for g in generator.generators:
        if isinstance(g, StaticPointGenerator):
            static_generator_axes.update(g.axes)
    axes_to_move = [a for a in axes_to_move if a not in static_generator_axes]
    return axes_to_move


async def cs_axis_mapping(motors: PmacMotors,
                          axes_to_move: Sequence[str]) -> Dict[str, MotorInfo]:
    """Given the layout table of a PMAC, create a MotorInfo for every axis in
    axes_to_move that isn't generated by a StaticPointGenerator. Check that they
    are all in the same CS"""
    cs_ports = set()  # type: Set[str]
    axis_mapping = {}  # type: Dict[str, MotorInfo]

    for motor in motors.iterator():
        cs = await motor.cs()
        name = await motor.scannable_name.get()
        if name in axes_to_move:
            cs_ports.add(cs.port)
            axis_mapping[name] = await motor_info(cs, name, motor)

    missing = list(set(axes_to_move) - set(axis_mapping))
    assert not missing, \
        "Some scannables %s are not in the CS mapping %s" % (
            missing, axis_mapping)
    assert len(cs_ports) == 1, \
        "Requested axes %s are in multiple CS numbers %s" % (
            axes_to_move, list(cs_ports))
    cs_axis_counts = Counter([x.cs.axis for x in axis_mapping.values()])
    # Any cs_axis defs that are used for more that one raw motor
    overlap = [k for k, v in cs_axis_counts.items() if v > 1]
    assert not overlap, \
        "CS axis defs %s have more that one raw motor attached" % overlap
    return axis_mapping


async def motor_info(cs: MotorCs, name: str, motor: PmacMotor) -> MotorInfo:
    assert cs.axis in CS_AXIS_NAMES, \
        "Can only scan 1-1 mappings, %r is %r" % (
            name, cs.axis)

    max_velocity, acceleration_time, resolution, offset, current_position, units = await get_all(
        motor.max_velocity,
        motor.acceleration_time,
        motor.resolution,
        motor.offset,
        motor.position,
        motor.units)

    acceleration = float(max_velocity) / acceleration_time
    return MotorInfo(
        cs=cs,
        acceleration=acceleration,
        resolution=resolution,
        offset=offset,
        max_velocity=max_velocity,
        current_position=current_position,
        scannable=name,
        velocity_settle=0.0,
        units=units
    )


def points_joined(axis_mapping, point, next_point):
    # type: (Dict[str, MotorInfo], Point, Point) -> bool
    """Check for axes that need to move within the space between points"""
    if getattr(point, "delay_after", None):
        return False
    for axis_name in axis_mapping:
        if point.upper[axis_name] != next_point.lower[axis_name]:
            return False

    return True


def point_velocities(axis_mapping, point, entry=True):
    # type: (Dict[str, MotorInfo], Point, bool) -> Dict[str, float]
    """Find the velocities of each axis over the entry/exit of current point"""
    velocities = {}
    for axis_name, motor_info in axis_mapping.items():
        #            x
        #        x       x
        #    x               x
        #    vl  vlp vp  vpu vu
        # Given distances from point,lower, position, upper, calculate
        # velocity at entry (vl) or exit (vu) of point by extrapolation
        dp = point.upper[axis_name] - point.lower[axis_name]
        vp = dp / point.duration
        if entry:
            # Halfway point is vlp, so calculate dlp
            d_half = point.positions[axis_name] - point.lower[axis_name]
        else:
            # Halfway point is vpu, so calculate dpu
            d_half = point.upper[axis_name] - point.positions[axis_name]
        # Extrapolate to get our entry or exit velocity
        # (vl + vp) / 2 = vlp
        # so vl = 2 * vlp - vp
        # where vlp = dlp / (t/2)
        velocity = 4 * d_half / point.duration - vp
        assert abs(velocity) < motor_info.max_velocity, \
            "Velocity %s invalid for %r with max_velocity %s" % (
                velocity, axis_name, motor_info.max_velocity)
        velocities[axis_name] = velocity
    return velocities


def get_user_program(output_triggers: MotionTrigger,
                     point_type: PointType) -> int:
    if output_triggers == MotionTrigger.NONE:
        # Always produce no program
        return UserPrograms.NO_PROGRAM
    elif output_triggers == MotionTrigger.ROW_GATE:
        if point_type == PointType.START_OF_ROW:
            # Produce a gate for the whole row
            return UserPrograms.LIVE_PROGRAM
        elif point_type == PointType.END_OF_ROW:
            # Falling edge of row gate
            return UserPrograms.ZERO_PROGRAM
        else:
            # Otherwise don't change anything
            return UserPrograms.NO_PROGRAM
    else:
        if point_type in (PointType.START_OF_ROW, PointType.POINT_JOIN):
            return UserPrograms.LIVE_PROGRAM
        elif point_type == PointType.END_OF_ROW:
            return UserPrograms.DEAD_PROGRAM
        elif point_type == PointType.MID_POINT:
            return UserPrograms.MID_PROGRAM
        else:
            return UserPrograms.ZERO_PROGRAM


def profile_between_points(
        axis_mapping,  # type: (Dict[str, MotorInfo])
        point,  # type: Point
        next_point,  # type: Point
        min_time=MIN_TIME,  # type: float
        min_interval=MIN_INTERVAL  # type: float
):
    """Make consistent time and velocity arrays for each axis
    Try to create velocity profiles for all axes that all arrive at
    'distance' in the same time period. The profiles will contain the
    following points:-
    in the following description acceleration can be -ve or +ve depending
    on the relative sign of v1 and v2. fabs(vm) is <= maximum velocity
    - start point at 0 secs with velocity v1     start accelerating
    - middle velocity start                      reached speed vm
    - middle velocity end                        start accelerating
    - end point with velocity v2                 reached target speed
    Time at vm may be 0 in which case there are only 3 points and
    acceleration to v2 starts as soon as vm is reached.
    If the profile has to be stretched to achieve min_time then the
    the middle period at speed vm is extended accordingly.
    After generating all the profiles this function checks to ensure they
    have all achieved min_time. If not min_time is reset to the slowest
    profile and all profiles are recalculated.
    Note that for each profile the area under the velocity/time plot
    must equal 'distance'. The class VelocityProfile implements the math
    to achieve this.
    """
    start_velocities = point_velocities(axis_mapping, point)
    end_velocities = point_velocities(axis_mapping, next_point, entry=False)

    p = None
    new_min_time = 0
    time_arrays = {}
    velocity_arrays = {}
    profiles = {}
    # The first iteration reveals the slowest profile. The second generates
    # all profiles with the slowest min_time
    iterations = 2
    while iterations > 0:
        for axis_name, motor_info in axis_mapping.items():
            distance = next_point.lower[axis_name] - point.upper[axis_name]
            p = motor_info.make_velocity_profile(
                start_velocities[axis_name], end_velocities[axis_name],
                distance, min_time, min_interval
            )
            # Absolute time values that we are at that velocity
            profiles[axis_name] = p
            new_min_time = max(new_min_time, p.t_total)
        if np.isclose(new_min_time, min_time):
            # We've got our consistent set - see if they require quantization
            quantize = False
            for axis_name, _ in axis_mapping.items():
                quantize = quantize or profiles[axis_name].check_quantize()
            for axis_name, _ in axis_mapping.items():
                if quantize:
                    profiles[axis_name].quantize()
                time_arrays[axis_name], velocity_arrays[axis_name] = \
                    profiles[axis_name].make_arrays()
            return time_arrays, velocity_arrays
        else:
            min_time = new_min_time
            iterations -= 1
    raise ValueError("Can't get a consistent time in 2 iterations")


def get_motion_trigger(part_info):
    # type: (scanning.hooks.APartInfo) -> scanning.infos.MotionTrigger
    # infos = MotionTriggerInfo.filter_values(part_info)
    # if infos:
    #     assert len(infos) == 1, \
    #         "Expected 0 or 1 MotionTriggerInfo, got %d" % len(infos)
    #     trigger = infos[0].trigger
    # else:
    #     trigger = MotionTrigger.EVERY_POINT
    # return trigger
    return MotionTrigger.EVERY_POINT
