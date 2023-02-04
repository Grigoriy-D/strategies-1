# Neural Network Binary Classifier: this subclass uses an NLinear model


import numpy as np
from pandas import DataFrame, Series
import pandas as pd

pd.options.mode.chained_assignment = None  # default='warn'

# Strategy specific imports, files must reside in same folder as strategy
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent))

import logging
import warnings

log = logging.getLogger(__name__)
# log.setLevel(logging.DEBUG)
warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)

import random

import os

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'
os.environ['TF_DETERMINISTIC_OPS'] = '1'

import tensorflow as tf
tf.config.set_visible_devices([], 'GPU')

seed = 42
os.environ['PYTHONHASHSEED'] = str(seed)
random.seed(seed)
np.random.seed(seed)

tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.WARN)

import keras
from keras import layers
from ClassifierDarts import ClassifierDarts
from darts.models import NLinearModel

import h5py


class NNPredictor_NLinear(ClassifierDarts):
    is_trained = False
    clean_data_required = False  # training data can contain anomalies
    model_per_pair = False  # separate model per pair

    # override the build_model function in subclasses
    def create_model(self, seq_len, num_features):
        # this model type has a tendency to exit early, so set min no. of epochs
        train_args = self.trainer_args
        train_args["min_epochs"] = 16
        model = NLinearModel(input_chunk_length=seq_len,
                             output_chunk_length=self.lookahead,
                             pl_trainer_kwargs=train_args
                             )
        return model

    # class-specific load
    def load_from_file(self, model_path):
        return NLinearModel.load(model_path)

