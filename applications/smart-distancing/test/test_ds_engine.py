import unittest
import doctest
import os
import sys

from typing import (
    Tuple,
    Optional,
    Iterator,
    Dict,
)

# base paths, and import setup
THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(THIS_DIR)
sys.path.insert(0, PARENT_DIR)

from smart_distancing.detectors.deepstream import _ds_engine
from smart_distancing.detectors.deepstream import _ds_config

# where to find test data
TEST_DATA_DIR = os.path.join(THIS_DIR, 'data')
VIDEO_FILENAMES = (
    'TownCentreXVID.avi',
)
#deepstream paths
SAMPLES_PATH = '/opt/nvidia/deepstream/deepstream/samples/'
MODEL_PATH =  os.path.join(SAMPLES_PATH, 'models')
CONFIGS_PATH = os.path.join(SAMPLES_PATH, 'configs')

# config generator for uridecodebin
FILE_SOURCE_CONFIGS = [
    {'uri': f'file://{fn}'} for fn in VIDEO_FILENAMES
]  # type: List[Dict[str, str]]
# a list of paths to sample models

# Configs for nvinfer elements in primary mode:
DETECTOR_CONFIGS = [
    {
        'model-file': os.path.join(MODEL_PATH, 'Primary_Detector/resnet10.caffemodel'),
        'proto-file': os.path.join(MODEL_PATH, 'Primary_Detector/resnet10.prototxt'),
        'labelfile-path': os.path.join(MODEL_PATH, 'Primary_Detector/labels.txt'),
        'int8-calib-file': os.path.join(MODEL_PATH, 'Primary_Detector/cal_trt.bin'),
    },
    {
        'model-file': os.path.join(MODEL_PATH, 'Primary_Detector_Nano/resnet10.caffemodel'),
        'proto-file': os.path.join(MODEL_PATH, 'Primary_Detector_Nano/resnet10.prototxt'),
        'labelfile-path': os.path.join(MODEL_PATH, 'Primary_Detector_Nano/labels.txt'),
    }
]  # type: List[Dict[str, str]]

CLASSIFIER_MODEL_DIRS = (
    'Secondary_CarColor',
    'Secondary_CarMake',
    'Secondary_VehicleTypes',
)
CLASSIFIER_MODELS = [
    [
        os.path.join(d, 'resnet18.caffemodel'),
        os.path.join(d, 'resnet18.prototxt'),
        os.path.join(d, 'labels.txt'),
        os.path.join(d, 'cal_trt.bin'),
        os.path.join(d, 'mean.ppm'),
    ] for d in CLASSIFIER_MODEL_DIRS
]
# we test every primary model with every secondary model
# with async mode both on and off
CLASSIFIER_CONFIGS = []
for detector_config in DETECTOR_CONFIGS:
    for async_mode in range(1):
        for c, p, l, i, m in CLASSIFIER_MODELS:
            conf = [
                detector_config,  # the primary config
                {
                    'model-file': m,
                    'proto-file': p,
                    'lablefile-path': l,
                    'int8-calib-file': i,
                    'mean-file': m, # bad file. very unfriendly (groans)
                    'classifier-async-mode': async_mode,
                },
            ]
            CLASSIFIER_CONFIGS.append(conf)

class TestDsEngine(unittest.TestCase):

    def test_doctests(self):
        """test none of the doctests fail"""
        # FIXME(mdegans):
        #  * there is no return code but there should be, since this is actually
        #    a fail since there is no uri supplied and GstEngine now uses
        #    uridecodebin since that has a sometimes pad. it's passing, but it shouldn't.
        self.assertEqual(
            doctest.testmod(_ds_engine, optionflags=doctest.ELLIPSIS)[0],
            0,
        )

    def test_start_stop(self):
        """test pipeline construct/destruct"""
        # this is copypasta from the TestGstEngine doctest
        single_detector_configs = [DETECTOR_CONFIGS[0],]
        single_file_configs = [FILE_SOURCE_CONFIGS[0],]

        config = _ds_config.DsConfig(single_detector_configs, single_file_configs)
        engine = _ds_engine.DsEngine(config)
        engine.start()
        engine.stop()
        engine.join(10)
        self.assertEqual(0, engine.exitcode)

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
