# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Builds the MNIST network.

Implements factory method create_model(). The function creates class
implementing MNIST specific implementations of build_train_graph(),
build_eval_graph(), build_prediction_graph() and format_metric_values().
"""

import argparse
import json
import logging

import tensorflow as tf
from tensorflow.contrib import layers
from tensorflow.contrib.metrics.python.ops import metric_ops
import util
from util import override_if_not_in_args

# Hyper-parameters
HIDDEN1 = 128  # Number of units in hidden layer 1.
HIDDEN2 = 32  # Number of units in hidden layer 2.

# The MNIST dataset has 10 classes, representing the digits 0 through 9.
NUM_CLASSES = 10

# The MNIST images are always 28x28 pixels.
IMAGE_SIZE = 28
IMAGE_PIXELS = IMAGE_SIZE * IMAGE_SIZE


def create_model():
  """Factory method that creates model to be used by generic task.py."""
  parser = argparse.ArgumentParser()
  parser.add_argument('--learning_rate', type=float, default=0.01)
  args, task_args = parser.parse_known_args()

  override_if_not_in_args('--max_steps', '5000', task_args)
  override_if_not_in_args('--batch_size', '100', task_args)
  override_if_not_in_args('--eval_set_size', '10000', task_args)
  override_if_not_in_args('--eval_interval_secs', '1', task_args)
  override_if_not_in_args('--log_interval_secs', '1', task_args)
  override_if_not_in_args('--min_train_eval_rate', '1', task_args)

  return Model(args.learning_rate, HIDDEN1, HIDDEN2), task_args


class GraphReferences(object):
  """Holder of base tensors used for training model using common task."""

  def __init__(self):
    self.examples = None
    self.train = None
    self.global_step = None
    self.metric_updates = []
    self.metric_values = []
    self.keys = None
    self.predictions = []


class Model(object):
  """TensorFlow model for the MNIST problem."""

  def __init__(self, learning_rate, hidden1, hidden2):
    self.learning_rate = learning_rate
    self.hidden1 = hidden1
    self.hidden2 = hidden2

  def build_graph(self, data_paths, batch_size, is_training):
    """Builds generic graph for training or eval."""
    tensors = GraphReferences()

    _, tensors.examples = util.read_examples(
        data_paths,
        batch_size,
        shuffle=is_training,
        num_epochs=None if is_training else 2)

    parsed = parse_examples(tensors.examples)

    # Build a Graph that computes predictions from the inference model.
    logits = inference(parsed['images'], self.hidden1, self.hidden2)

    # Add to the Graph the Ops for loss calculation.
    loss_value = loss(logits, parsed['labels'])

    # Add to the Graph the Ops for accuracy calculation.
    accuracy_value = evaluation(logits, parsed['labels'])

    # Add to the Graph the Ops that calculate and apply gradients.
    if is_training:
      tensors.train, tensors.global_step = training(loss_value,
                                                    self.learning_rate)
    else:
      tensors.global_step = tf.Variable(0, name='global_step', trainable=False)

    # Add streaming means.
    loss_op, loss_update = metric_ops.streaming_mean(loss_value)
    accuracy_op, accuracy_update = metric_ops.streaming_mean(accuracy_value)

    tf.scalar_summary('accuracy', accuracy_op)
    tf.scalar_summary('loss', loss_op)

    tensors.metric_updates = [loss_update, accuracy_update]
    tensors.metric_values = [loss_op, accuracy_op]
    return tensors

  def build_train_graph(self, data_paths, batch_size):
    return self.build_graph(data_paths, batch_size, is_training=True)

  def build_eval_graph(self, data_paths, batch_size):
    return self.build_graph(data_paths, batch_size, is_training=False)

  def build_prediction_graph(self, export_dir):
    """Builds prediction graph and registers appropriate endpoints."""
    logging.info('Exporting prediction graph to %s', export_dir)
    examples = tf.placeholder(tf.string, shape=(None,))
    features = {
        'image': tf.FixedLenFeature(
            shape=[IMAGE_PIXELS], dtype=tf.float32),
        'key': tf.FixedLenFeature(
            shape=[], dtype=tf.string),
    }

    parsed = tf.parse_example(examples, features)
    images = parsed['image']
    keys = parsed['key']

    # Build a Graph that computes predictions from the inference model.
    logits = inference(images, self.hidden1, self.hidden2)
    softmax = tf.nn.softmax(logits)
    prediction = tf.argmax(softmax, 1)

    # Mark the inputs and the outputs
    # Marking the input tensor with an alias with suffix _bytes. This is to
    # indicate that this tensor value is raw bytes and will be base64 encoded
    # over HTTP.
    # Note that any output tensor marked with an alias with suffix _bytes, shall
    # be base64 encoded in the HTTP response. To get the binary value, it
    # should be base64 decoded.
    tf.add_to_collection('inputs',
                         json.dumps({'examples_bytes': examples.name}))
    tf.add_to_collection('outputs', json.dumps({
        'key': keys.name,
        'prediction': prediction.name,
        'scores': softmax.name
    }))

  def format_metric_values(self, metric_values):
    """Formats metric values - used for logging purpose."""
    return 'loss: %.3f, accuracy: %.3f' % (metric_values[0], metric_values[1])

  def format_prediction_values(self, prediction):
    """Formats prediction values - used for writing batch predictions as csv."""
    return '%.3f' % (prediction[0])


def parse_examples(examples):
  feature_map = {
      'labels': tf.FixedLenFeature(
          shape=[], dtype=tf.int64, default_value=[-1]),
      'images': tf.FixedLenFeature(
          shape=[IMAGE_PIXELS], dtype=tf.float32),
  }
  return tf.parse_example(examples, features=feature_map)


def inference(images, hidden1_units, hidden2_units):
  """Build the MNIST model up to where it may be used for inference.

  Args:
    images: Images placeholder, from inputs().
    hidden1_units: Size of the first hidden layer.
    hidden2_units: Size of the second hidden layer.
  Returns:
    softmax_linear: Output tensor with the computed logits.
  """
  hidden1 = layers.fully_connected(images, hidden1_units)
  hidden2 = layers.fully_connected(hidden1, hidden2_units)
  return layers.fully_connected(hidden2, NUM_CLASSES)


def loss(logits, labels):
  """Calculates the loss from the logits and the labels.

  Args:
    logits: Logits tensor, float - [batch_size, NUM_CLASSES].
    labels: Labels tensor, int32 - [batch_size].
  Returns:
    loss: Loss tensor of type float.
  """
  labels = tf.to_int64(labels)
  cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
      logits, labels, name='xentropy')
  return tf.reduce_mean(cross_entropy, name='xentropy_mean')


def training(loss_op, learning_rate):
  """Sets up the training Ops.

  Creates a summarizer to track the loss over time in TensorBoard.
  Creates an optimizer and applies the gradients to all trainable variables.
  The Op returned by this function is what must be passed to the
  `sess.run()` call to cause the model to train.
  Args:
    loss_op: Loss tensor, from loss().
    learning_rate: The learning rate to use for gradient descent.
  Returns:
    A pair consisting of the Op for training and the global step.
  """
  # Create the gradient descent optimizer with the given learning rate.
  optimizer = tf.train.GradientDescentOptimizer(learning_rate)
  # Create a variable to track the global step.
  global_step = tf.Variable(0, name='global_step', trainable=False)
  # Use the optimizer to apply the gradients that minimize the loss
  # (and also increment the global step counter) as a single training step.
  train_op = optimizer.minimize(loss_op, global_step=global_step)
  return train_op, global_step


def evaluation(logits, labels):
  """Evaluate the quality of the logits at predicting the label.

  Args:
    logits: Logits tensor, float - [batch_size, NUM_CLASSES].
    labels: Labels tensor, int32 - [batch_size], with values in the
      range [0, NUM_CLASSES).
  Returns:
    A scalar float tensor with the ratio of examples (out of batch_size)
    that were predicted correctly.
  """
  # For a classifier model, we can use the in_top_k Op.
  # It returns a bool tensor with shape [batch_size] that is true for
  # the examples where the label is in the top k (here k=1)
  # of all logits for that example.
  correct = tf.nn.in_top_k(logits, labels, 1)
  return tf.reduce_mean(tf.cast(correct, tf.float32))
