# Neural Network Binary Classifier: this subclass uses a simple Multi-Layer Perceptron model


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
tf.random.set_seed(seed)
np.random.seed(seed)

tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.WARN)

import keras
from keras import layers
from ClassifierKerasBinary import ClassifierKerasBinary

import h5py


class NNBClassifier_MLP(ClassifierKerasBinary):
    is_trained = False
    clean_data_required = True  # training data cannot contain anomalies

    # override the build_model function in subclasses
    def create_model(self, seq_len, num_features):

        model = keras.Sequential(name=self.name)

        # very simple MLP model:
        model.add(layers.Dense(128, input_shape=(seq_len, num_features)))
        model.add(layers.Dropout(rate=0.1))
        model.add(layers.Dense(32))
        model.add(layers.Dropout(rate=0.1))

        # last layer is a binary decision - do not change
        model.add(layers.Dense(1, activation='sigmoid'))

        return model
