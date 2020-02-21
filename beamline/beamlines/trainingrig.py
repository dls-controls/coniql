import asyncio

from dataclasses import dataclass

from coniql.deviceplugin import DevicePlugin
from device.channel.setup import setup
from device.devices.addetector import AdDetector
from device.devices.adpanda import AdPandA
from device.devices.camera import Camera
from device.devices.pmac import Pmac, AxisMotors
from device.devices.stage3d import Stage3D
from device.devices.tomostage import TomoStage
from device.epics.ad import camera
from device.epics.addets import ad_detector, ad_panda
from device.epics.motor import motor, pmac_motor
from device.epics.pmac import pmac


# def adsim_device_environment():
#     beamline = adsim_environment('ws415')
#
#     plugin = DevicePlugin()
#     plugin.register_device(beamline, name='beamline')
#
#     plugin.debug()
#     return plugin


@dataclass
class TrainingRig:
    detector: AdDetector
    panda_position_detector: AdPandA
    sample_stage: TomoStage
    pmac: Pmac


async def p47_environment():
    return await training_rig_environment('BL47P')


async def training_rig_environment(beamline_prefix: str) -> TrainingRig:
    x = pmac_motor(f'{beamline_prefix}-MO-MAP-01:STAGE:X')
    theta = pmac_motor(f'{beamline_prefix}-MO-MAP-01:STAGE:A')
    sample_stage = TomoStage(x, theta)
    det = ad_detector(f'{beamline_prefix}-EA-DET-01', cam_prefix='DET')
    panda_det = ad_panda(f'{beamline_prefix}-MO-PANDA-01')
    axis_motors = AxisMotors(x=x, a=theta)
    pmc = pmac(f'{beamline_prefix}-MO-BRICK-01', axis_motors)
    beamline = TrainingRig(
        detector=det,
        panda_position_detector=panda_det,
        sample_stage=sample_stage,
        pmac=pmc
    )
    await setup(beamline)
    return beamline