# Neural Network Trinary Classifier: this subclass uses a Transformer model


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

seed = 42
os.environ['PYTHONHASHSEED'] = str(seed)
random.seed(seed)
tf.random.set_seed(seed)
np.random.seed(seed)

tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.WARN)

import keras
from keras import layers
from ClassifierKerasTrinary import ClassifierKerasTrinary



class NNTClassifier_Transformer(ClassifierKerasTrinary):
    is_trained = False
    clean_data_required = False  # training data cannot contain anomalies

    # override the build_model function in subclasses
    def create_model(self, seq_len, num_features):

        head_size = num_features
        num_heads = int(num_features / 2)
        # ff_dim = 4
        ff_dim = seq_len
        num_transformer_blocks = seq_len
        mlp_units = [64, 32, 8]
        mlp_dropout = 0.2
        dropout = 0.2

        inputs = keras.Input(shape=(seq_len, num_features))
        x = inputs
        for _ in range(num_transformer_blocks):
            x = self.transformer_encoder(x, head_size, num_heads, dropout, ff_dim)
            x = keras.layers.BatchNormalization()(x)

        # x = layers.GlobalAveragePooling1D(keepdims=True, data_format="channels_first")(x)

        for dim in mlp_units:
            # x = layers.Dense(dim, activation="relu")(x)
            x = layers.Dense(dim)(x)
            x = layers.Dropout(mlp_dropout)(x)

        # last layer is a trinary decision - do not change
        outputs = layers.Dense(3, activation="softmax")(x)

        model = keras.Model(inputs, outputs)

        return model


    def transformer_encoder(self, inputs, head_size, num_heads, dropout, ff_dim):

        # Normalization and Attention
        x = layers.LayerNormalization(epsilon=1e-6)(inputs)
        x = layers.MultiHeadAttention(key_dim=head_size, num_heads=num_heads, dropout=dropout)(x, x)
        x = layers.Dropout(dropout)(x)

        res = x + inputs

        # Feed Forward Part
        x = layers.LayerNormalization(epsilon=1e-6)(res)
        x = layers.Conv1D(filters=ff_dim, kernel_size=1, activation="relu")(x)
        x = layers.Dropout(dropout)(x)
        # x = layers.Conv1D(filters=inputs.shape[-1], kernel_size=1)(x)
        x = layers.Conv1D(filters=head_size, kernel_size=1)(x)
        return x + res
