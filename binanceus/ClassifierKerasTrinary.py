# base class that implements Neural Network Trinary Classifier.
# This class implements a keras classifier that provides a trinary result (nothing, buy, sell)

# subclasses should override the create_model() method
from enum import Enum

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

# workaround for memory leak in tensorflow 2.10
os.environ['TF_RUN_EAGER_OP_AS_FUNCTION'] = '0'

import tensorflow as tf

seed = 42
os.environ['PYTHONHASHSEED'] = str(seed)
random.seed(seed)
tf.random.set_seed(seed)
np.random.seed(seed)

tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.WARN)

import keras
from keras import layers
from keras.losses import CategoricalCrossentropy

import h5py

from DataframeUtils import DataframeUtils
from ClassifierKeras import ClassifierKeras


# enum for results
class Result(Enum):
    NOTHING = 0
    BUY = 1
    SELL = 2


class ClassifierKerasTrinary(ClassifierKeras):

    clean_data_required = False

    # create model - subclasses should overide this
    def create_model(self, seq_len, num_features):

        model = None

        print("    WARNING: create_model() should be defined by the subclass")

        # create a simple model for illustrative purposes (or to test the framework)
        model = keras.Sequential(name=self.name)

        # NOTE: don't use relu with LSTMs, cannot use GPU if you do (much slower). Use tanh

        # simplest possible model:
        model.add(layers.LSTM(128, return_sequences=True, activation='tanh', input_shape=(seq_len, num_features)))
        model.add(layers.Dropout(rate=0.1))

        # last layer is a trinary decision - do not change
        model.add(layers.Dense(3, activation='softmax'))

        return model

    def compile_model(self, model):

        # optimizer = keras.optimizers.Adam(learning_rate=0.001)
        optimizer = keras.optimizers.Adam(learning_rate=0.01)

        # must use binary_crossentropy loss because this is a binary classifier
        model.compile(optimizer=optimizer,
                      loss=CategoricalCrossentropy(),
                      metrics=['accuracy', 'mse'])

        return model

    # update training using the suplied (normalised) dataframe. Training is cumulative
    # the 'labels' args should contain 0.0 for normal results, '1.0' for buys, 2.0 for sells
    def train(self, df_train_norm, df_test_norm, train_results, test_results, force_train=False):

        # lazy loading because params can change up to this point
        if self.model is None:
            # load saved model if present
            self.model = self.load()

        # print(f'is_trained:{self.is_trained} force_train:{force_train}')

        # if model is already trained, and caller is not requesting a re-train, then just return
        if (self.model is not None) and self.model_is_trained() and (not force_train) and (not self.new_model_created()):
            # print(f"    Not training. is_trained:{self.is_trained} force_train:{force_train} new_model:{self.new_model}")
            print("    Model is already trained")
            return

        if self.model is None:
            self.model = self.create_model(self.seq_len, self.num_features)
            if self.model is None:
                print("    ERR: model not created")
                return
            self.model = self.compile_model(self.model)
            self.model.summary()

        if self.dataframeUtils.is_dataframe(df_train_norm):
            # remove rows with positive labels?!
            if self.clean_data_required:
                df1 = df_train_norm.copy()
                df1['%labels'] = train_results
                df1 = df1[(df1['%labels'] < 0.1)]
                df_train = df1.drop('%labels', axis=1)

                df2 = df_train_norm.copy()
                df2['%labels'] = train_results
                df2 = df2[(df2['%labels'] < 0.1)]
                df_test = df2.drop('%labels', axis=1)
            else:
                df_train = df_train_norm.copy()
                df_test = df_test_norm.copy()

            train_tensor = self.dataframeUtils.df_to_tensor(df_train, self.seq_len)
            test_tensor = self.dataframeUtils.df_to_tensor(df_test, self.seq_len)
        else:
            # already in tensor format
            train_tensor = df_train_norm.copy()
            test_tensor = df_test_norm.copy()

        monitor_field = 'loss'
        monitor_mode = "min"
        early_patience = 4
        plateau_patience = 4

        # callback to control early exit on plateau of results
        early_callback = keras.callbacks.EarlyStopping(
            monitor=monitor_field,
            mode=monitor_mode,
            patience=early_patience,
            min_delta=0.0001,
            restore_best_weights=True,
            verbose=0)

        plateau_callback = keras.callbacks.ReduceLROnPlateau(
            monitor=monitor_field,
            mode=monitor_mode,
            factor=0.1,
            min_delta=0.0001,
            patience=plateau_patience,
            verbose=0)

        # callback to control saving of 'best' model
        # Note that we use validation loss as the metric, not training loss
        checkpoint_callback = keras.callbacks.ModelCheckpoint(
            filepath=self.checkpoint_path,
            save_weights_only=True,
            monitor=monitor_field,
            mode=monitor_mode,
            save_best_only=True,
            verbose=0)

        callbacks = [plateau_callback, early_callback, checkpoint_callback]

        keras.backend.set_value(self.model.optimizer.learning_rate, 0.01)

        # if self.dbg_verbose:
        print("")
        print("    training model: {}...".format(self.name))

        # print("    train_tensor:{} test_tensor:{}".format(np.shape(train_tensor), np.shape(test_tensor)))

        # Model weights are saved at the end of every epoch, if it's the best seen so far.
        fhis = self.model.fit(train_tensor, train_results,
                                    batch_size=self.batch_size,
                                    epochs=self.num_epochs,
                                    callbacks=callbacks,
                                    validation_data=(test_tensor, test_results),
                                    verbose=1)

        # # The model weights (that are considered the best) are loaded into th model.
        # self.update_model_weights()

        self.save()
        self.is_trained = True

        return


    def predict(self, data):

        # lazy loading because params can change up to this point
        if self.model is None:
            # load saved model if present
            self.model = self.load()

        if self.dataframeUtils.is_dataframe(data):
            # convert dataframe to tensor
            df_tensor = self.dataframeUtils.df_to_tensor(data, self.seq_len)
        else:
            df_tensor = data

        if self.model == None:
            print("    ERR: no model for predictions")
            predictions = np.zeros(np.shape(df_tensor)[0], dtype=float)
            return predictions

        # run the prediction
        preds = self.model.predict(df_tensor, verbose=0)

        # re-shape into a vector
        preds = preds[:, 0]
        # print(f'preds: {np.shape(preds)} {preds}')

        # convert softmax result into a trinary value
        predictions = np.argmax(preds, axis=1) # softmax output

        return predictions

