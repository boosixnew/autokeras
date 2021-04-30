# Copyright 2020 The AutoKeras Authors.
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
import collections
import math
import re
import unicodedata
from typing import List

import numpy as np
import six
import tensorflow as tf
from tensorflow.keras.layers.experimental import preprocessing
from tensorflow.python.util import nest

from autokeras import constants
from autokeras.engine import preprocessor
from autokeras.utils import data_utils

INT = "int"
NONE = "none"
ONE_HOT = "one-hot"


@tf.keras.utils.register_keras_serializable()
class CastToFloat32(preprocessing.PreprocessingLayer):
    def call(self, inputs):
        return data_utils.cast_to_float32(inputs)


@tf.keras.utils.register_keras_serializable()
class ExpandLastDim(preprocessing.PreprocessingLayer):
    def call(self, inputs):
        return tf.expand_dims(inputs, axis=-1)


@tf.keras.utils.register_keras_serializable()
class MultiCategoryEncoding(preprocessing.PreprocessingLayer):
    """Encode the categorical features to numerical features.

    # Arguments
        encoding: A list of strings, which has the same number of elements as the
            columns in the structured data. Each of the strings specifies the
            encoding method used for the corresponding column. Use 'int' for
            categorical columns and 'none' for numerical columns.
    """

    # TODO: Support one-hot encoding.
    # TODO: Support frequency encoding.

    def __init__(self, encoding: List[str], **kwargs):
        super().__init__(**kwargs)
        self.encoding = encoding
        self.encoding_layers = []
        for encoding in self.encoding:
            if encoding == NONE:
                self.encoding_layers.append(None)
            elif encoding == INT:
                self.encoding_layers.append(preprocessing.StringLookup())
            elif encoding == ONE_HOT:
                self.encoding_layers.append(None)

    def build(self, input_shape):
        for encoding_layer in self.encoding_layers:
            if encoding_layer is not None:
                encoding_layer.build(tf.TensorShape([1]))

    def call(self, inputs):
        input_nodes = nest.flatten(inputs)[0]
        split_inputs = tf.split(input_nodes, [1] * len(self.encoding), axis=-1)
        output_nodes = []
        for input_node, encoding_layer in zip(split_inputs, self.encoding_layers):
            if encoding_layer is None:
                number = data_utils.cast_to_float32(input_node)
                # Replace NaN with 0.
                imputed = tf.where(
                    tf.math.is_nan(number), tf.zeros_like(number), number
                )
                output_nodes.append(imputed)
            else:
                output_nodes.append(
                    data_utils.cast_to_float32(
                        encoding_layer(data_utils.cast_to_string(input_node))
                    )
                )
        if len(output_nodes) == 1:
            return output_nodes[0]
        return tf.keras.layers.Concatenate()(output_nodes)

    def adapt(self, data):
        for index, encoding_layer in enumerate(self.encoding_layers):
            if encoding_layer is None:
                continue
            data_column = data.map(lambda x: tf.slice(x, [0, index], [-1, 1]))
            encoding_layer.adapt(data_column.map(data_utils.cast_to_string))

    def get_config(self):
        config = {
            "encoding": self.encoding,
        }
        base_config = super().get_config()
        return dict(list(base_config.items()) + list(config.items()))


# TODO: Remove after KerasNLP is ready.
@tf.keras.utils.register_keras_serializable()
class BertTokenizer(preprocessing.PreprocessingLayer):
    """Vectorization and Encoding the sentences using BERT vocabulary.

    # Arguments
        max_sequence_length: maximum length of the sequences after vectorization.
    """

    def __init__(self, max_sequence_length: int, **kwargs):
        super().__init__(**kwargs)
        self.tokenizer = FullTokenizer(
            vocab_file=constants.BERT_VOCAB_PATH,
            do_lower_case=True,
        )
        self.max_sequence_length = max_sequence_length

    def get_config(self):
        config = super().get_config()
        config.update({"max_sequence_length": self.max_sequence_length})
        return config

    def build(self, input_shape):
        self.batch_size = input_shape

    def call(self, inputs):
        input_word_ids = tf.numpy_function(
            func=self.bert_encode, inp=[inputs], Tout=tf.int32
        )
        input_word_ids.set_shape((None, None))
        input_mask = tf.zeros_like(input_word_ids)

        input_type_ids = tf.zeros_like(input_word_ids)

        return input_word_ids, input_mask, input_type_ids

    def encode_sentence(self, s):
        """Encodes a sentence using the BERT tokenizer.

        Tokenizes, and adjusts the sentence length to the maximum sequence
        length. Some important tokens in the BERT tokenizer are:
        [UNK]: 100, [CLS]: 101, [SEP]: 102, [MASK]: 103.

        # Arguments
            s: Tensor. Raw sentence string.
        """
        tokens = list(self.tokenizer.tokenize(s))
        tokens.append("[SEP]")
        encoded_sentence = self.tokenizer.convert_tokens_to_ids(tokens)
        return encoded_sentence

    def get_encoded_sentence(self, input_tensor):
        input_array = np.array(input_tensor, dtype=object)
        sentence = tf.ragged.constant(
            [self.encode_sentence(s[0]) for s in input_array]
        )
        return sentence

    def bert_encode(self, input_tensor):
        sentence = self.get_encoded_sentence(input_tensor)
        cls = [self.tokenizer.convert_tokens_to_ids(["[CLS]"])] * sentence.shape[0]
        input_word_ids = tf.concat([cls, sentence], axis=-1).to_tensor()
        if input_word_ids.shape[-1] > self.max_sequence_length:
            input_word_ids = input_word_ids[..., : self.max_sequence_length]

        return input_word_ids


# TODO: Remove after KerasNLP is ready.
@tf.keras.utils.register_keras_serializable()
class BertEncoder(tf.keras.layers.Layer):
    """Cleaned up official.nlp.modeling.networks.TransformerEncoder."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        embedding_width = 768
        dropout_rate = 0.1
        initializer = tf.keras.initializers.TruncatedNormal(stddev=0.02)

        self._embedding_layer = OnDeviceEmbedding(
            vocab_size=30522,
            embedding_width=embedding_width,
            initializer=initializer,
            name="word_embeddings",
        )

        # Always uses dynamic slicing for simplicity.
        self._position_embedding_layer = PositionEmbedding(
            initializer=initializer,
            use_dynamic_slicing=True,
            max_sequence_length=512,
            name="position_embedding",
        )
        self._type_embedding_layer = OnDeviceEmbedding(
            vocab_size=2,
            embedding_width=embedding_width,
            initializer=initializer,
            use_one_hot=True,
            name="type_embeddings",
        )
        self._add = tf.keras.layers.Add()
        self._layer_norm = tf.keras.layers.LayerNormalization(
            name="embeddings/layer_norm", axis=-1, epsilon=1e-12, dtype=tf.float32
        )
        self._dropout = tf.keras.layers.Dropout(rate=dropout_rate)

        self._attention_mask = SelfAttentionMask()
        self._transformer_layers = []
        for i in range(12):
            layer = Transformer(
                num_attention_heads=12,
                intermediate_size=3072,
                intermediate_activation=gelu,
                dropout_rate=dropout_rate,
                attention_dropout_rate=0.1,
                output_range=None,
                kernel_initializer=initializer,
                name="transformer/layer_%d" % i,
            )
            self._transformer_layers.append(layer)

        self._lambda = tf.keras.layers.Lambda(
            lambda x: tf.squeeze(x[:, 0:1, :], axis=1)
        )
        self._pooler_layer = tf.keras.layers.Dense(
            units=embedding_width,
            activation="tanh",
            kernel_initializer=initializer,
            name="pooler_transform",
        )

    def call(self, inputs):
        word_ids = inputs[0]
        mask = inputs[1]
        type_ids = inputs[2]
        word_embeddings = self._embedding_layer(word_ids)
        position_embeddings = self._position_embedding_layer(word_embeddings)
        type_embeddings = self._type_embedding_layer(type_ids)

        embeddings = self._add(
            [word_embeddings, position_embeddings, type_embeddings]
        )

        embeddings = self._layer_norm(embeddings)
        embeddings = self._dropout(embeddings)
        data = embeddings
        attention_mask = self._attention_mask([data, mask])
        encoder_outputs = []
        for layer in self._transformer_layers:
            data = layer([data, attention_mask])
            encoder_outputs.append(data)

        first_token_tensor = self._lambda(encoder_outputs[-1])

        cls_output = self._pooler_layer(first_token_tensor)

        return cls_output

    def load_pretrained_weights(self):
        checkpoint = tf.train.Checkpoint(model=self)
        checkpoint.restore(constants.BERT_CHECKPOINT_PATH).assert_consumed()


@tf.keras.utils.register_keras_serializable()
class AdamWeightDecay(tf.keras.optimizers.Adam):
    """official.nlp.optimization.AdamWeightDecay"""

    def __init__(
        self,
        learning_rate=0.001,
        beta_1=0.9,
        beta_2=0.999,
        epsilon=1e-7,
        amsgrad=False,
        weight_decay_rate=0.0,
        include_in_weight_decay=None,
        exclude_from_weight_decay=None,
        name="AdamWeightDecay",
        **kwargs
    ):
        super(AdamWeightDecay, self).__init__(
            learning_rate, beta_1, beta_2, epsilon, amsgrad, name, **kwargs
        )
        self.weight_decay_rate = weight_decay_rate
        self._include_in_weight_decay = include_in_weight_decay
        self._exclude_from_weight_decay = exclude_from_weight_decay

    def _prepare_local(self, var_device, var_dtype, apply_state):
        super(AdamWeightDecay, self)._prepare_local(
            var_device, var_dtype, apply_state
        )
        apply_state[(var_device, var_dtype)]["weight_decay_rate"] = tf.constant(
            self.weight_decay_rate, name="adam_weight_decay_rate"
        )

    def _decay_weights_op(self, var, learning_rate, apply_state):
        do_decay = self._do_use_weight_decay(var.name)
        if do_decay:
            return var.assign_sub(
                learning_rate
                * var
                * apply_state[(var.device, var.dtype.base_dtype)][
                    "weight_decay_rate"
                ],
                use_locking=self._use_locking,
            )
        return tf.no_op()

    def apply_gradients(
        self, grads_and_vars, name=None, experimental_aggregate_gradients=True
    ):
        grads, tvars = list(zip(*grads_and_vars))
        if experimental_aggregate_gradients:
            # when experimental_aggregate_gradients = False, apply_gradients() no
            # longer implicitly allreduce gradients, users manually allreduce
            # gradient and passed the allreduced grads_and_vars. For now, the
            # clip_by_global_norm will be moved to before the explicit allreduce to
            # keep the math the same as TF 1 and pre TF 2.2 implementation.
            (grads, _) = tf.clip_by_global_norm(
                grads, clip_norm=1.0
            )  # pragma: no cover
        return super(AdamWeightDecay, self).apply_gradients(
            zip(grads, tvars),
            name=name,
            experimental_aggregate_gradients=experimental_aggregate_gradients,
        )

    def _get_lr(self, var_device, var_dtype, apply_state):
        """Retrieves the learning rate with the given state."""
        if apply_state is None:
            return self._decayed_lr_t[var_dtype], {}  # pragma: no cover

        apply_state = apply_state or {}
        coefficients = apply_state.get((var_device, var_dtype))
        if coefficients is None:
            coefficients = self._fallback_apply_state(
                var_device, var_dtype
            )  # pragma: no cover
            apply_state[(var_device, var_dtype)] = coefficients  # pragma: no cover

        return coefficients["lr_t"], dict(apply_state=apply_state)

    def _resource_apply_dense(self, grad, var, apply_state=None):
        lr_t, kwargs = self._get_lr(var.device, var.dtype.base_dtype, apply_state)
        decay = self._decay_weights_op(var, lr_t, apply_state)
        with tf.control_dependencies([decay]):
            return super(AdamWeightDecay, self)._resource_apply_dense(
                grad, var, **kwargs
            )

    def _resource_apply_sparse(self, grad, var, indices, apply_state=None):
        lr_t, kwargs = self._get_lr(
            var.device, var.dtype.base_dtype, apply_state
        )  # pragma: no cover
        decay = self._decay_weights_op(var, lr_t, apply_state)  # pragma: no cover
        with tf.control_dependencies([decay]):  # pragma: no cover
            return super(
                AdamWeightDecay, self  # pragma: no cover
            )._resource_apply_sparse(grad, var, indices, **kwargs)

    def get_config(self):
        config = super(AdamWeightDecay, self).get_config()
        config.update(
            {
                "weight_decay_rate": self.weight_decay_rate,
            }
        )
        return config

    def _do_use_weight_decay(self, param_name):
        """Whether to use L2 weight decay for `param_name`."""
        if self._exclude_from_weight_decay:
            for r in self._exclude_from_weight_decay:
                if re.search(r, param_name) is not None:
                    return False
        return True


@tf.keras.utils.register_keras_serializable()
class WarmUp(tf.keras.optimizers.schedules.LearningRateSchedule):
    """official.nlp.optimization.WarmUp"""

    def __init__(
        self,
        initial_learning_rate,
        decay_schedule_fn,
        warmup_steps,
        power=1.0,
        name=None,
    ):
        super(WarmUp, self).__init__()
        self.initial_learning_rate = initial_learning_rate
        self.warmup_steps = warmup_steps
        self.power = power
        self.decay_schedule_fn = decay_schedule_fn
        self.name = name

    def __call__(self, step):
        with tf.name_scope(self.name or "WarmUp") as name:
            # Implements polynomial warmup. i.e., if global_step < warmup_steps, the
            # learning rate will be `global_step/num_warmup_steps * init_lr`.
            global_step_float = tf.cast(step, tf.float32)
            warmup_steps_float = tf.cast(self.warmup_steps, tf.float32)
            warmup_percent_done = global_step_float / warmup_steps_float
            warmup_learning_rate = self.initial_learning_rate * tf.math.pow(
                warmup_percent_done, self.power
            )
            return tf.cond(
                global_step_float < warmup_steps_float,
                lambda: warmup_learning_rate,
                lambda: self.decay_schedule_fn(step),
                name=name,
            )

    def get_config(self):
        return {
            "initial_learning_rate": self.initial_learning_rate,
            "decay_schedule_fn": self.decay_schedule_fn,
            "warmup_steps": self.warmup_steps,
            "power": self.power,
            "name": self.name,
        }


@tf.keras.utils.register_keras_serializable()
def gelu(x):
    """official.modeling.activations.gelu"""
    cdf = 0.5 * (
        1.0 + tf.tanh((math.sqrt(2 / math.pi) * (x + 0.044715 * tf.pow(x, 3))))
    )
    return x * cdf


@tf.keras.utils.register_keras_serializable()
class OnDeviceEmbedding(tf.keras.layers.Layer):
    """official.nlp.modeling.layers.OnDeviceEmbedding"""

    def __init__(
        self,
        vocab_size,
        embedding_width,
        initializer="glorot_uniform",
        use_one_hot=False,
        **kwargs
    ):

        super(OnDeviceEmbedding, self).__init__(**kwargs)
        self._vocab_size = vocab_size
        self._embedding_width = embedding_width
        self._initializer = initializer
        self._use_one_hot = use_one_hot

    def get_config(self):
        config = {
            "vocab_size": self._vocab_size,
            "embedding_width": self._embedding_width,
            "initializer": self._initializer,
            "use_one_hot": self._use_one_hot,
        }
        base_config = super(OnDeviceEmbedding, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def build(self, input_shape):
        self.embeddings = self.add_weight(
            "embeddings",
            shape=[self._vocab_size, self._embedding_width],
            initializer=self._initializer,
            dtype=tf.float32,
        )

        super(OnDeviceEmbedding, self).build(input_shape)

    def call(self, inputs):
        flat_inputs = tf.reshape(inputs, [-1])
        if self._use_one_hot:
            one_hot_data = tf.one_hot(
                flat_inputs, depth=self._vocab_size, dtype=self.embeddings.dtype
            )
            embeddings = tf.matmul(one_hot_data, self.embeddings)
        else:
            embeddings = tf.gather(self.embeddings, flat_inputs)
        embeddings = tf.reshape(
            embeddings,
            # Work around b/142213824: prefer concat to shape over a Python list.
            tf.concat([tf.shape(inputs), [self._embedding_width]], axis=0),
        )
        embeddings.set_shape(inputs.shape.as_list() + [self._embedding_width])
        return embeddings


@tf.keras.utils.register_keras_serializable()
class PositionEmbedding(tf.keras.layers.Layer):
    """official.nlp.modeling.layers.PositionEmbedding"""

    def __init__(
        self,
        initializer="glorot_uniform",
        use_dynamic_slicing=False,
        max_sequence_length=None,
        **kwargs
    ):
        # We need to have a default dtype of float32, since the inputs (which Keras
        # usually uses to infer the dtype) will always be int32.
        if "dtype" not in kwargs:
            kwargs["dtype"] = "float32"

        super(PositionEmbedding, self).__init__(**kwargs)
        if use_dynamic_slicing and max_sequence_length is None:
            raise ValueError(  # pragma: no cover
                "If `use_dynamic_slicing` is True, "
                "`max_sequence_length` must be set."
            )
        self._max_sequence_length = max_sequence_length
        self._initializer = tf.keras.initializers.get(initializer)
        self._use_dynamic_slicing = use_dynamic_slicing

    def get_config(self):
        config = {
            "max_sequence_length": self._max_sequence_length,
            "initializer": tf.keras.initializers.serialize(self._initializer),
            "use_dynamic_slicing": self._use_dynamic_slicing,
        }
        base_config = super(PositionEmbedding, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def build(self, input_shape):
        """Implements build() for the layer."""
        dimension_list = input_shape.as_list()

        if len(dimension_list) != 3:
            raise ValueError(  # pragma: no cover
                "PositionEmbedding expects a 3-dimensional input tensor "
                "of shape [batch, sequence, width]"
            )
        seq_length = dimension_list[1]
        width = dimension_list[2]

        # If we are not using dynamic slicing, we must assume that the sequence
        # length is fixed and max_sequence_length should not be specified.
        if not self._use_dynamic_slicing:
            if seq_length is None:  # pragma: no cover
                raise ValueError(  # pragma: no cover
                    "PositionEmbedding must have `use_dynamic_slicing` set "
                    "to True (and max_sequence_length set) when the "
                    "sequence (1st) dimension of the input is None."
                )
            if self._max_sequence_length is not None:  # pragma: no cover
                raise ValueError(  # pragma: no cover
                    "When `use_dynamic_slicing` is False, "
                    "max_sequence_length should "
                    "not be specified and we ought to use seq_length to get the "
                    "variable shape."
                )

        if self._max_sequence_length is not None:
            weight_sequence_length = self._max_sequence_length
        else:
            weight_sequence_length = seq_length  # pragma: no cover

        self._position_embeddings = self.add_weight(
            "embeddings",
            shape=[weight_sequence_length, width],
            initializer=self._initializer,
        )

        super(PositionEmbedding, self).build(input_shape)

    def call(self, inputs):
        """Implements call() for the layer."""
        input_shape = get_shape_list(inputs, expected_rank=3)
        if self._use_dynamic_slicing:
            position_embeddings = self._position_embeddings[: input_shape[1], :]
        else:
            position_embeddings = self._position_embeddings  # pragma: no cover

        return tf.broadcast_to(position_embeddings, input_shape)


def get_shape_list(tensor, expected_rank=None, name=None):
    """official.modeling.tf_utils.get_shape_list"""
    if expected_rank is not None:
        assert_rank(tensor, expected_rank, name)

    shape = tensor.shape.as_list()

    non_static_indexes = []
    for (index, dim) in enumerate(shape):
        if dim is None:
            non_static_indexes.append(index)

    if not non_static_indexes:
        return shape  # pragma: no cover

    dyn_shape = tf.shape(tensor)
    for index in non_static_indexes:
        shape[index] = dyn_shape[index]
    return shape


def assert_rank(tensor, expected_rank, name=None):
    """official.modeling.tf_utils.assert_rank"""
    expected_rank_dict = {}
    if isinstance(expected_rank, six.integer_types):
        expected_rank_dict[expected_rank] = True
    else:
        for x in expected_rank:
            expected_rank_dict[x] = True

    actual_rank = tensor.shape.ndims
    if actual_rank not in expected_rank_dict:
        raise ValueError(  # pragma: no cover
            "For the tensor `%s`, the actual tensor rank `%d` (shape = %s) is not "
            "equal to the expected tensor rank `%s`"
            % (name, actual_rank, str(tensor.shape), str(expected_rank))
        )


@tf.keras.utils.register_keras_serializable()
class SelfAttentionMask(tf.keras.layers.Layer):
    """official.nlp.modeling.layers.SelfAttentionMask"""

    def call(self, inputs):
        from_tensor = inputs[0]
        to_mask = inputs[1]
        from_shape = get_shape_list(from_tensor, expected_rank=[2, 3])
        batch_size = from_shape[0]
        from_seq_length = from_shape[1]

        to_shape = get_shape_list(to_mask, expected_rank=2)
        to_seq_length = to_shape[1]

        to_mask = tf.cast(
            tf.reshape(to_mask, [batch_size, 1, to_seq_length]),
            dtype=from_tensor.dtype,
        )

        # We don't assume that `from_tensor` is a mask (although it could be). We
        # don't actually care if we attend *from* padding tokens (only *to* padding)
        # tokens so we create a tensor of all ones.
        #
        # `broadcast_ones` = [batch_size, from_seq_length, 1]
        broadcast_ones = tf.ones(
            shape=[batch_size, from_seq_length, 1], dtype=from_tensor.dtype
        )

        # Here we broadcast along two dimensions to create the mask.
        mask = broadcast_ones * to_mask

        return mask


@tf.keras.utils.register_keras_serializable()
class Transformer(tf.keras.layers.Layer):
    """official.nlp.modeling.layers.Transformer"""

    def __init__(
        self,
        num_attention_heads,
        intermediate_size,
        intermediate_activation,
        dropout_rate=0.0,
        attention_dropout_rate=0.0,
        output_range=None,
        kernel_initializer="glorot_uniform",
        bias_initializer="zeros",
        kernel_regularizer=None,
        bias_regularizer=None,
        activity_regularizer=None,
        kernel_constraint=None,
        bias_constraint=None,
        **kwargs
    ):
        super(Transformer, self).__init__(**kwargs)

        self._num_heads = num_attention_heads
        self._intermediate_size = intermediate_size
        self._intermediate_activation = intermediate_activation
        self._attention_dropout_rate = attention_dropout_rate
        self._dropout_rate = dropout_rate
        self._output_range = output_range
        self._kernel_initializer = tf.keras.initializers.get(kernel_initializer)
        self._bias_initializer = tf.keras.initializers.get(bias_initializer)
        self._kernel_regularizer = tf.keras.regularizers.get(kernel_regularizer)
        self._bias_regularizer = tf.keras.regularizers.get(bias_regularizer)
        self._activity_regularizer = tf.keras.regularizers.get(activity_regularizer)
        self._kernel_constraint = tf.keras.constraints.get(kernel_constraint)
        self._bias_constraint = tf.keras.constraints.get(bias_constraint)

    def build(self, input_shape):
        input_tensor = input_shape[0] if len(input_shape) == 2 else input_shape
        input_tensor_shape = tf.TensorShape(input_tensor)
        if len(input_tensor_shape) != 3:
            raise ValueError(  # pragma: no cover
                "TransformerLayer expects a three-dimensional input of "
                "shape [batch, sequence, width]."
            )
        batch_size, sequence_length, hidden_size = input_tensor_shape

        if len(input_shape) == 2:
            mask_tensor_shape = tf.TensorShape(input_shape[1])
            expected_mask_tensor_shape = tf.TensorShape(
                [batch_size, sequence_length, sequence_length]
            )
            if not expected_mask_tensor_shape.is_compatible_with(mask_tensor_shape):
                raise ValueError(  # pragma: no cover
                    "When passing a mask tensor to TransformerLayer, the "
                    "mask tensor must be of shape [batch, "
                    "sequence_length, sequence_length] (here %s). Got a "
                    "mask tensor of shape %s."
                    % (expected_mask_tensor_shape, mask_tensor_shape)
                )
        if hidden_size % self._num_heads != 0:
            raise ValueError(  # pragma: no cover
                "The input size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (hidden_size, self._num_heads)
            )
        self._attention_head_size = int(hidden_size // self._num_heads)

        self._attention_layer = MultiHeadAttention(
            num_heads=self._num_heads,
            key_size=self._attention_head_size,
            dropout=self._attention_dropout_rate,
            kernel_initializer=self._kernel_initializer,
            bias_initializer=self._bias_initializer,
            kernel_regularizer=self._kernel_regularizer,
            bias_regularizer=self._bias_regularizer,
            activity_regularizer=self._activity_regularizer,
            kernel_constraint=self._kernel_constraint,
            bias_constraint=self._bias_constraint,
            name="self_attention",
        )
        # pylint: disable=protected-access
        self._attention_layer.build([input_tensor_shape] * 3)
        self._attention_output_dense = self._attention_layer._output_dense
        # pylint: enable=protected-access
        self._attention_dropout = tf.keras.layers.Dropout(rate=self._dropout_rate)
        # Use float32 in layernorm for numeric stability.
        # It is probably safe in mixed_float16, but we haven't validated this yet.
        self._attention_layer_norm = tf.keras.layers.LayerNormalization(
            name="self_attention_layer_norm",
            axis=-1,
            epsilon=1e-12,
            dtype=tf.float32,
        )
        self._intermediate_dense = DenseEinsum(
            output_shape=self._intermediate_size,
            activation=None,
            kernel_initializer=self._kernel_initializer,
            bias_initializer=self._bias_initializer,
            kernel_regularizer=self._kernel_regularizer,
            bias_regularizer=self._bias_regularizer,
            activity_regularizer=self._activity_regularizer,
            kernel_constraint=self._kernel_constraint,
            bias_constraint=self._bias_constraint,
            name="intermediate",
        )
        policy = tf.keras.mixed_precision.experimental.global_policy()
        if policy.name == "mixed_bfloat16":
            # bfloat16 causes BERT with the LAMB optimizer to not converge
            # as well, so we use float32.
            # TODO(b/154538392): Investigate this.
            policy = tf.float32  # pragma: no cover
        self._intermediate_activation_layer = tf.keras.layers.Activation(
            self._intermediate_activation, dtype=policy
        )
        self._output_dense = DenseEinsum(
            output_shape=hidden_size,
            kernel_initializer=self._kernel_initializer,
            bias_initializer=self._bias_initializer,
            kernel_regularizer=self._kernel_regularizer,
            bias_regularizer=self._bias_regularizer,
            activity_regularizer=self._activity_regularizer,
            kernel_constraint=self._kernel_constraint,
            bias_constraint=self._bias_constraint,
            name="output",
        )
        self._output_dropout = tf.keras.layers.Dropout(rate=self._dropout_rate)
        # Use float32 in layernorm for numeric stability.
        self._output_layer_norm = tf.keras.layers.LayerNormalization(
            name="output_layer_norm", axis=-1, epsilon=1e-12, dtype=tf.float32
        )

        super(Transformer, self).build(input_shape)

    def get_config(self):
        config = {
            "num_attention_heads": self._num_heads,
            "intermediate_size": self._intermediate_size,
            "intermediate_activation": self._intermediate_activation,
            "dropout_rate": self._dropout_rate,
            "attention_dropout_rate": self._attention_dropout_rate,
            "output_range": self._output_range,
            "kernel_initializer": tf.keras.initializers.serialize(
                self._kernel_initializer
            ),
            "bias_initializer": tf.keras.initializers.serialize(
                self._bias_initializer
            ),
            "kernel_regularizer": tf.keras.regularizers.serialize(
                self._kernel_regularizer
            ),
            "bias_regularizer": tf.keras.regularizers.serialize(
                self._bias_regularizer
            ),
            "activity_regularizer": tf.keras.regularizers.serialize(
                self._activity_regularizer
            ),
            "kernel_constraint": tf.keras.constraints.serialize(
                self._kernel_constraint
            ),
            "bias_constraint": tf.keras.constraints.serialize(self._bias_constraint),
        }
        base_config = super(Transformer, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def call(self, inputs):
        if isinstance(inputs, (list, tuple)) and len(inputs) == 2:
            input_tensor, attention_mask = inputs
        else:
            input_tensor, attention_mask = (inputs, None)  # pragma: no cover

        if self._output_range:
            target_tensor = input_tensor[
                :, 0 : self._output_range, :
            ]  # pragma: no cover
            attention_mask = attention_mask[
                :, 0 : self._output_range, :
            ]  # pragma: no cover
        else:
            target_tensor = input_tensor
        attention_inputs = [target_tensor, input_tensor]

        attention_output = self._attention_layer(attention_inputs, attention_mask)
        attention_output = self._attention_dropout(attention_output)
        attention_output = self._attention_layer_norm(
            target_tensor + attention_output
        )
        intermediate_output = self._intermediate_dense(attention_output)
        intermediate_output = self._intermediate_activation_layer(
            intermediate_output
        )
        layer_output = self._output_dense(intermediate_output)
        layer_output = self._output_dropout(layer_output)
        # During mixed precision training, attention_output is from layer norm and
        # is always fp32 for now. Cast layer_output to fp32 for the subsequent
        # add.
        layer_output = tf.cast(layer_output, tf.float32)
        layer_output = self._output_layer_norm(layer_output + attention_output)

        return layer_output


EinsumDense = tf.keras.layers.experimental.EinsumDense


@tf.keras.utils.register_keras_serializable()
class MultiHeadAttention(tf.keras.layers.Layer):
    """official.nlp.modeling.layers.attention.MultiHeadAttention"""

    def __init__(
        self,
        num_heads,
        key_size,
        value_size=None,
        dropout=0.0,
        use_bias=True,
        output_shape=None,
        attention_axes=None,
        return_attention_scores=False,
        kernel_initializer="glorot_uniform",
        bias_initializer="zeros",
        kernel_regularizer=None,
        bias_regularizer=None,
        activity_regularizer=None,
        kernel_constraint=None,
        bias_constraint=None,
        **kwargs
    ):
        super(MultiHeadAttention, self).__init__(**kwargs)
        self._num_heads = num_heads
        self._key_size = key_size
        self._value_size = value_size if value_size else key_size
        self._dropout = dropout
        self._use_bias = use_bias
        self._output_shape = output_shape
        self._return_attention_scores = return_attention_scores
        self._kernel_initializer = tf.keras.initializers.get(kernel_initializer)
        self._bias_initializer = tf.keras.initializers.get(bias_initializer)
        self._kernel_regularizer = tf.keras.regularizers.get(kernel_regularizer)
        self._bias_regularizer = tf.keras.regularizers.get(bias_regularizer)
        self._kernel_constraint = tf.keras.constraints.get(kernel_constraint)
        self._bias_constraint = tf.keras.constraints.get(bias_constraint)
        if attention_axes is not None and not isinstance(
            attention_axes, collections.abc.Sized
        ):
            self._attention_axes = (attention_axes,)  # pragma: no cover
        else:
            self._attention_axes = attention_axes

    def get_config(self):
        config = {
            "num_heads": self._num_heads,
            "key_size": self._key_size,
            "value_size": self._value_size,
            "dropout": self._dropout,
            "use_bias": self._use_bias,
            "output_shape": self._output_shape,
            "attention_axes": self._attention_axes,
            "return_attention_scores": self._return_attention_scores,
            "kernel_initializer": tf.keras.initializers.serialize(
                self._kernel_initializer
            ),
            "bias_initializer": tf.keras.initializers.serialize(
                self._bias_initializer
            ),
            "kernel_regularizer": tf.keras.regularizers.serialize(
                self._kernel_regularizer
            ),
            "bias_regularizer": tf.keras.regularizers.serialize(
                self._bias_regularizer
            ),
            "activity_regularizer": tf.keras.regularizers.serialize(
                self._activity_regularizer
            ),
            "kernel_constraint": tf.keras.constraints.serialize(
                self._kernel_constraint
            ),
            "bias_constraint": tf.keras.constraints.serialize(self._bias_constraint),
        }
        base_config = super(MultiHeadAttention, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def build(self, input_shape):
        inputs_len = len(input_shape)
        if inputs_len > 3 or inputs_len < 2:
            raise ValueError(  # pragma: no cover
                "Expects inputs list of length 2 or 3, namely [query, value] or "
                "[query, value, key]. "
                "Given length: %d" % inputs_len
            )
        tensor_shapes = tf.nest.map_structure(tf.TensorShape, input_shape)
        query_shape = tensor_shapes[0]
        value_shape = tensor_shapes[1]
        key_shape = tensor_shapes[2] if inputs_len == 3 else value_shape

        common_kwargs = dict(
            kernel_initializer=self._kernel_initializer,
            bias_initializer=self._bias_initializer,
            kernel_regularizer=self._kernel_regularizer,
            bias_regularizer=self._bias_regularizer,
            activity_regularizer=self._activity_regularizer,
            kernel_constraint=self._kernel_constraint,
            bias_constraint=self._bias_constraint,
        )

        free_dims = query_shape.rank - 1
        einsum_equation, bias_axes, output_rank = _build_proj_equation(
            free_dims, bound_dims=1, output_dims=2
        )
        self._query_dense = EinsumDense(
            einsum_equation,
            output_shape=_get_output_shape(
                output_rank - 1, [self._num_heads, self._key_size]
            ),
            bias_axes=bias_axes if self._use_bias else None,
            name="query",
            **common_kwargs
        )
        einsum_equation, bias_axes, output_rank = _build_proj_equation(
            key_shape.rank - 1, bound_dims=1, output_dims=2
        )
        self._key_dense = EinsumDense(
            einsum_equation,
            output_shape=_get_output_shape(
                output_rank - 1, [self._num_heads, self._key_size]
            ),
            bias_axes=bias_axes if self._use_bias else None,
            name="key",
            **common_kwargs
        )
        einsum_equation, bias_axes, output_rank = _build_proj_equation(
            value_shape.rank - 1, bound_dims=1, output_dims=2
        )
        self._value_dense = EinsumDense(
            einsum_equation,
            output_shape=_get_output_shape(
                output_rank - 1, [self._num_heads, self._value_size]
            ),
            bias_axes=bias_axes if self._use_bias else None,
            name="value",
            **common_kwargs
        )

        # Builds the attention computations for multi-head dot product attention.
        # These computations could be wrapped into the keras attention layer once it
        # support mult-head einsum computations.
        self._build_attention(output_rank)
        if self._output_shape:
            if not isinstance(
                self._output_shape, collections.abc.Sized
            ):  # pragma: no cover
                output_shape = [self._output_shape]  # pragma: no cover
            else:
                output_shape = self._output_shape  # pragma: no cover
        else:
            output_shape = [query_shape[-1]]
        einsum_equation, bias_axes, output_rank = _build_proj_equation(
            free_dims, bound_dims=2, output_dims=len(output_shape)
        )
        self._output_dense = EinsumDense(
            einsum_equation,
            output_shape=_get_output_shape(output_rank - 1, output_shape),
            bias_axes=bias_axes if self._use_bias else None,
            name="attention_output",
            **common_kwargs
        )
        super(MultiHeadAttention, self).build(input_shape)

    def _build_attention(self, qkv_rank):
        """Builds multi-head dot-product attention computations.

        This function builds attributes necessary for `_compute_attention` to
        costomize attention computation to replace the default dot-product
        attention.

        Args:
          qkv_rank: the rank of query, key, value tensors.
        """
        if self._attention_axes is None:
            self._attention_axes = tuple(range(1, qkv_rank - 2))
        else:
            self._attention_axes = tuple(self._attention_axes)  # pragma: no cover
        (
            self._dot_product_equation,
            self._combine_equation,
            attn_scores_rank,
        ) = _build_attention_equation(qkv_rank, attn_axes=self._attention_axes)
        norm_axes = tuple(
            range(attn_scores_rank - len(self._attention_axes), attn_scores_rank)
        )
        self._masked_softmax = MaskedSoftmax(
            mask_expansion_axes=[1], normalization_axes=norm_axes
        )
        self._dropout_layer = tf.keras.layers.Dropout(rate=self._dropout)

    def _compute_attention(
        self, query_tensor, key_tensor, value_tensor, attention_mask=None
    ):
        """Applies Dot-product attention with query, key, value tensors.

        This function defines the computation inside `call` with projected
        multi-head Q, K, V inputs. Users can override this function for customized
        attention implementation.

        Args:
          query_tensor: Projected query `Tensor` of shape `[B, T, N, key_size]`.
          key_tensor: Projected key `Tensor` of shape `[B, T, N, key_size]`.
          value_tensor: Projected value `Tensor` of shape `[B, T, N, value_size]`.
          attention_mask: a boolean mask of shape `[B, T, S]`, that prevents
            attention to certain positions.

        Returns:
          attention_output: Multi-headed outputs of attention computation.
          attention_scores: Multi-headed attention weights.
        """
        # Take the dot product between "query" and "key" to get the raw
        # attention scores.
        attention_scores = tf.einsum(
            self._dot_product_equation, key_tensor, query_tensor
        )
        attention_scores = tf.multiply(
            attention_scores, 1.0 / math.sqrt(float(self._key_size))
        )

        # Normalize the attention scores to probabilities.
        # `attention_scores` = [B, N, T, S]
        attention_scores = self._masked_softmax(attention_scores, attention_mask)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_scores_dropout = self._dropout_layer(attention_scores)

        # `context_layer` = [B, T, N, H]
        attention_output = tf.einsum(
            self._combine_equation, attention_scores_dropout, value_tensor
        )
        return attention_output, attention_scores

    def call(self, inputs, attention_mask=None):
        """Implements the forward pass.

        Size glossary:
          * Number of heads (H): the number of attention heads.
          * Value size (V): the size of each value embedding per head.
          * Key size (K): the size of each key embedding per head. Equally, the size
              of each query embedding per head. Typically K <= V.
          * Batch dimensions (B).
          * Query (target) attention axes shape (T).
          * Value (source) attention axes shape (S), the rank must match the target.

        Args:
          inputs: List of the following tensors:
            * query: Query `Tensor` of shape `[B, T, dim]`.
            * value: Value `Tensor` of shape `[B, S, dim]`.
            * key: Optional key `Tensor` of shape `[B, S, dim]`. If not given, will
              use `value` for both `key` and `value`, which is the most common case.
          attention_mask: a boolean mask of shape `[B, T, S]`, that prevents
            attention to certain positions.

        Returns:
          attention_output: The result of the computation, of shape [B, T, E],
            where `T` is for target sequence shapes and `E` is the query input last
            dimension if `output_shape` is `None`. Otherwise, the multi-head outputs
            are project to the shape specified by `output_shape`.
          attention_scores: [Optional] multi-head attention coeffients over
          attention
            axes.
        """
        inputs_len = len(inputs)
        if inputs_len > 3 or inputs_len < 2:
            raise ValueError(  # pragma: no cover
                "Expects inputs list of length 2 or 3, namely [query, value] or "
                "[query, value, key]. "
                "Given length: %d" % inputs_len
            )
        query = inputs[0]
        value = inputs[1]
        key = inputs[2] if inputs_len == 3 else value

        #   N = `num_attention_heads`
        #   H = `size_per_head`
        # `query_tensor` = [B, T, N ,H]
        query_tensor = self._query_dense(query)

        # `key_tensor` = [B, S, N, H]
        key_tensor = self._key_dense(key)

        # `value_tensor` = [B, S, N, H]
        value_tensor = self._value_dense(value)

        attention_output, attention_scores = self._compute_attention(
            query_tensor, key_tensor, value_tensor, attention_mask
        )
        attention_output = self._output_dense(attention_output)

        if self._return_attention_scores:
            return attention_output, attention_scores  # pragma: no cover
        return attention_output


@tf.keras.utils.register_keras_serializable()
class DenseEinsum(tf.keras.layers.Layer):
    """from official.nlp.modeling.layers.dense_einsum.DenseEinsum"""

    def __init__(
        self,
        output_shape,
        num_summed_dimensions=1,
        activation=None,
        use_bias=True,
        kernel_initializer="glorot_uniform",
        bias_initializer="zeros",
        kernel_regularizer=None,
        bias_regularizer=None,
        activity_regularizer=None,
        kernel_constraint=None,
        bias_constraint=None,
        **kwargs
    ):
        super(DenseEinsum, self).__init__(**kwargs)
        self._output_shape = (
            output_shape
            if isinstance(output_shape, (list, tuple))
            else (output_shape,)
        )
        self._activation = tf.keras.activations.get(activation)
        self._use_bias = use_bias
        self._kernel_initializer = tf.keras.initializers.get(kernel_initializer)
        self._bias_initializer = tf.keras.initializers.get(bias_initializer)
        self._kernel_regularizer = tf.keras.regularizers.get(kernel_regularizer)
        self._bias_regularizer = tf.keras.regularizers.get(bias_regularizer)
        self._kernel_constraint = tf.keras.constraints.get(kernel_constraint)
        self._bias_constraint = tf.keras.constraints.get(bias_constraint)
        self._num_summed_dimensions = num_summed_dimensions
        self._einsum_string = None

    def _build_einsum_string(self, free_input_dims, bound_dims, output_dims):
        _CHR_IDX = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m"]
        input_str = ""
        kernel_str = ""
        output_str = ""
        letter_offset = 0
        for i in range(free_input_dims):
            char = _CHR_IDX[i + letter_offset]
            input_str += char
            output_str += char

        letter_offset += free_input_dims
        for i in range(bound_dims):
            char = _CHR_IDX[i + letter_offset]
            input_str += char
            kernel_str += char

        letter_offset += bound_dims
        for i in range(output_dims):
            char = _CHR_IDX[i + letter_offset]
            kernel_str += char
            output_str += char

        return input_str + "," + kernel_str + "->" + output_str

    def build(self, input_shape):
        input_shape = tf.TensorShape(input_shape)
        input_rank = input_shape.rank
        free_input_dims = input_rank - self._num_summed_dimensions
        output_dims = len(self._output_shape)

        self._einsum_string = self._build_einsum_string(
            free_input_dims, self._num_summed_dimensions, output_dims
        )

        # This is only saved for testing purposes.
        self._kernel_shape = input_shape[free_input_dims:].concatenate(
            self._output_shape
        )

        self._kernel = self.add_weight(
            "kernel",
            shape=self._kernel_shape,
            initializer=self._kernel_initializer,
            regularizer=self._kernel_regularizer,
            constraint=self._kernel_constraint,
            dtype=self.dtype,
            trainable=True,
        )
        if self._use_bias:
            self._bias = self.add_weight(
                "bias",
                shape=self._output_shape,
                initializer=self._bias_initializer,
                regularizer=self._bias_regularizer,
                constraint=self._bias_constraint,
                dtype=self.dtype,
                trainable=True,
            )
        else:
            self._bias = None  # pragma: no cover
        super(DenseEinsum, self).build(input_shape)

    def get_config(self):
        config = {
            "output_shape": self._output_shape,
            "num_summed_dimensions": self._num_summed_dimensions,
            "activation": tf.keras.activations.serialize(self._activation),
            "use_bias": self._use_bias,
            "kernel_initializer": tf.keras.initializers.serialize(
                self._kernel_initializer
            ),
            "bias_initializer": tf.keras.initializers.serialize(
                self._bias_initializer
            ),
            "kernel_regularizer": tf.keras.regularizers.serialize(
                self._kernel_regularizer
            ),
            "bias_regularizer": tf.keras.regularizers.serialize(
                self._bias_regularizer
            ),
            "activity_regularizer": tf.keras.regularizers.serialize(
                self._activity_regularizer
            ),
            "kernel_constraint": tf.keras.constraints.serialize(
                self._kernel_constraint
            ),
            "bias_constraint": tf.keras.constraints.serialize(self._bias_constraint),
        }
        base_config = super(DenseEinsum, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def call(self, inputs):
        ret = tf.einsum(self._einsum_string, inputs, self._kernel)
        if self._use_bias:
            ret += self._bias
        if self._activation is not None:
            ret = self._activation(ret)
        return ret


def _build_proj_equation(free_dims, bound_dims, output_dims):
    """Builds an einsum equation for projections inside multi-head attention."""
    import string

    _CHR_IDX = string.ascii_lowercase
    input_str = ""
    kernel_str = ""
    output_str = ""
    bias_axes = ""
    letter_offset = 0
    for i in range(free_dims):
        char = _CHR_IDX[i + letter_offset]
        input_str += char
        output_str += char

    letter_offset += free_dims
    for i in range(bound_dims):
        char = _CHR_IDX[i + letter_offset]
        input_str += char
        kernel_str += char

    letter_offset += bound_dims
    for i in range(output_dims):
        char = _CHR_IDX[i + letter_offset]
        kernel_str += char
        output_str += char
        bias_axes += char
    equation = "%s,%s->%s" % (input_str, kernel_str, output_str)

    return equation, bias_axes, len(output_str)


def _get_output_shape(output_rank, known_last_dims):
    return [None] * (output_rank - len(known_last_dims)) + list(known_last_dims)


def _build_attention_equation(qkv_rank, attn_axes):
    """Builds einsum equations for the attention computation.

    Query, key, value inputs after projection are expected to have the shape as:
    (bs, <non-attention dims>, <attention dims>, num_heads, channels).
    bs and <non-attention dims> are treated as <batch dims>.
    The attention operations can be generalized:
    (1) Query-key dot product:
    (<batch dims>, <query attention dims>, num_heads, channels), (<batch dims>,
    <key attention dims>, num_heads, channels) -> (<batch dims>,
    num_heads, <query attention dims>, <key attention dims>)
    (2) Combination:
    (<batch dims>, num_heads, <query attention dims>, <key attention dims>),
    (<batch dims>, <value attention dims>, num_heads, channels) -> (<batch dims>,
    <query attention dims>, num_heads, channels)

    Args:
      qkv_rank: the rank of query, key, value tensors.
      attn_axes: a list/tuple of axes, [1, rank), that will do attention.

    Returns:
      Einsum equations.
    """
    import string

    _CHR_IDX = string.ascii_lowercase
    target_notation = _CHR_IDX[:qkv_rank]
    # `batch_dims` includes the head dim.
    batch_dims = tuple(np.delete(range(qkv_rank), attn_axes + (qkv_rank - 1,)))
    letter_offset = qkv_rank
    source_notation = ""
    for i in range(qkv_rank):
        if i in batch_dims or i == qkv_rank - 1:
            source_notation += target_notation[i]
        else:
            source_notation += _CHR_IDX[letter_offset]
            letter_offset += 1

    product_notation = "".join(
        [target_notation[i] for i in batch_dims]
        + [target_notation[i] for i in attn_axes]
        + [source_notation[i] for i in attn_axes]
    )
    dot_product_equation = "%s,%s->%s" % (
        source_notation,
        target_notation,
        product_notation,
    )
    attn_scores_rank = len(product_notation)
    combine_equation = "%s,%s->%s" % (
        product_notation,
        source_notation,
        target_notation,
    )
    return dot_product_equation, combine_equation, attn_scores_rank


@tf.keras.utils.register_keras_serializable()
class MaskedSoftmax(tf.keras.layers.Layer):
    """Performs a softmax with optional masking on a tensor.

    Args:
      mask_expansion_axes: Any axes that should be padded on the mask tensor.
      normalization_axes: On which axes the softmax should perform.
    """

    def __init__(self, mask_expansion_axes=None, normalization_axes=None, **kwargs):
        self._mask_expansion_axes = mask_expansion_axes
        if normalization_axes is None:
            self._normalization_axes = (-1,)  # pragma: no cover
        else:
            self._normalization_axes = normalization_axes
        super(MaskedSoftmax, self).__init__(**kwargs)

    def call(self, scores, mask=None):
        if mask is not None:
            for _ in range(len(scores.shape) - len(mask.shape)):
                mask = tf.expand_dims(mask, axis=self._mask_expansion_axes)

            # Since attention_mask is 1.0 for positions we want to attend and 0.0 for
            # masked positions, this operation will create a tensor which is 0.0 for
            # positions we want to attend and -10000.0 for masked positions.
            adder = (1.0 - tf.cast(mask, scores.dtype)) * -10000.0

            # Since we are adding it to the raw scores before the softmax, this is
            # effectively the same as removing these entirely.
            scores += adder

        if len(self._normalization_axes) == 1:
            return tf.nn.softmax(scores, axis=self._normalization_axes[0])
        else:
            return tf.math.exp(  # pragma: no cover
                scores
                - tf.math.reduce_logsumexp(
                    scores, axis=self._normalization_axes, keepdims=True
                )
            )

    def get_config(self):
        config = {
            "mask_expansion_axes": self._mask_expansion_axes,
            "normalization_axes": self._normalization_axes,
        }
        base_config = super(MaskedSoftmax, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


def convert_by_vocab(vocab, items):
    """Converts a sequence of [tokens|ids] using the vocab."""
    output = []
    for item in items:
        output.append(vocab[item])
    return output


def convert_to_unicode(text):
    """Converts `text` to Unicode (if it's not already), assuming utf-8 input."""
    if six.PY3:
        if isinstance(text, str):
            return text
        elif isinstance(text, bytes):
            return text.decode("utf-8", "ignore")
        else:
            raise ValueError(
                "Unsupported string type: %s" % (type(text))
            )  # pragma: no cover


def load_vocab(vocab_file):
    """Loads a vocabulary file into a dictionary."""
    vocab = collections.OrderedDict()
    index = 0
    with tf.io.gfile.GFile(vocab_file, "r") as reader:
        while True:
            token = convert_to_unicode(reader.readline())
            if not token:
                break
            token = token.strip()
            vocab[token] = index
            index += 1
    return vocab


class FullTokenizer(object):
    """Runs end-to-end tokenziation."""

    def __init__(self, vocab_file, do_lower_case=True, split_on_punc=True):
        self.vocab = load_vocab(vocab_file)
        self.inv_vocab = {v: k for k, v in self.vocab.items()}
        self.basic_tokenizer = BasicTokenizer(
            do_lower_case=do_lower_case, split_on_punc=split_on_punc
        )
        self.wordpiece_tokenizer = WordpieceTokenizer(vocab=self.vocab)

    def tokenize(self, text):
        split_tokens = []
        for token in self.basic_tokenizer.tokenize(text):
            for sub_token in self.wordpiece_tokenizer.tokenize(token):
                split_tokens.append(sub_token)

        return split_tokens

    def convert_tokens_to_ids(self, tokens):
        return convert_by_vocab(self.vocab, tokens)

    def convert_ids_to_tokens(self, ids):
        return convert_by_vocab(self.inv_vocab, ids)  # pragma: no cover


class BasicTokenizer(object):
    """Runs basic tokenization (punctuation splitting, lower casing, etc.)."""

    def __init__(self, do_lower_case=True, split_on_punc=True):
        """Constructs a BasicTokenizer.

        Args:
          do_lower_case: Whether to lower case the input.
          split_on_punc: Whether to apply split on punctuations. By default BERT
            starts a new token for punctuations. This makes detokenization difficult
            for tasks like seq2seq decoding.
        """
        self.do_lower_case = do_lower_case
        self.split_on_punc = split_on_punc

    def tokenize(self, text):
        """Tokenizes a piece of text."""
        text = convert_to_unicode(text)
        text = self._clean_text(text)

        # This was added on November 1st, 2018 for the multilingual and Chinese
        # models. This is also applied to the English models now, but it doesn't
        # matter since the English models were not trained on any Chinese data
        # and generally don't have any Chinese data in them (there are Chinese
        # characters in the vocabulary because Wikipedia does have some Chinese
        # words in the English Wikipedia.).
        text = self._tokenize_chinese_chars(text)

        orig_tokens = whitespace_tokenize(text)
        split_tokens = []
        for token in orig_tokens:
            if self.do_lower_case:
                token = token.lower()
                token = self._run_strip_accents(token)
            if self.split_on_punc:
                split_tokens.extend(self._run_split_on_punc(token))
            else:
                split_tokens.append(token)  # pragma: no cover

        output_tokens = whitespace_tokenize(" ".join(split_tokens))
        return output_tokens

    def _run_strip_accents(self, text):
        """Strips accents from a piece of text."""
        text = unicodedata.normalize("NFD", text)
        output = []
        for char in text:
            cat = unicodedata.category(char)
            if cat == "Mn":
                continue  # pragma: no cover
            output.append(char)
        return "".join(output)

    def _run_split_on_punc(self, text):
        """Splits punctuation on a piece of text."""
        chars = list(text)
        i = 0
        start_new_word = True
        output = []
        while i < len(chars):
            char = chars[i]
            if _is_punctuation(char):
                output.append([char])
                start_new_word = True
            else:
                if start_new_word:
                    output.append([])
                start_new_word = False
                output[-1].append(char)
            i += 1

        return ["".join(x) for x in output]

    def _tokenize_chinese_chars(self, text):
        """Adds whitespace around any CJK character."""
        output = []
        for char in text:
            cp = ord(char)
            if self._is_chinese_char(cp):
                output.append(" ")  # pragma: no cover
                output.append(char)  # pragma: no cover
                output.append(" ")  # pragma: no cover
            else:
                output.append(char)
        return "".join(output)

    def _is_chinese_char(self, cp):
        """Checks whether CP is the codepoint of a CJK character."""
        # This defines a "chinese character" as anything in the CJK Unicode block:
        #   https://en.wikipedia.org/wiki/CJK_Unified_Ideographs_(Unicode_block)
        #
        # Note that the CJK Unicode block is NOT all Japanese and Korean characters,
        # despite its name. The modern Korean Hangul alphabet is a different block,
        # as is Japanese Hiragana and Katakana. Those alphabets are used to write
        # space-separated words, so they are not treated specially and handled
        # like the all of the other languages.
        if (
            (cp >= 0x4E00 and cp <= 0x9FFF)
            or (cp >= 0x3400 and cp <= 0x4DBF)  #
            or (cp >= 0x20000 and cp <= 0x2A6DF)  #
            or (cp >= 0x2A700 and cp <= 0x2B73F)  #
            or (cp >= 0x2B740 and cp <= 0x2B81F)  #
            or (cp >= 0x2B820 and cp <= 0x2CEAF)  #
            or (cp >= 0xF900 and cp <= 0xFAFF)
            or (cp >= 0x2F800 and cp <= 0x2FA1F)  #
        ):  #
            return True  # pragma: no cover

        return False

    def _clean_text(self, text):
        """Performs invalid character removal and whitespace cleanup on text."""
        output = []
        for char in text:
            cp = ord(char)
            if cp == 0 or cp == 0xFFFD or _is_control(char):
                continue  # pragma: no cover
            if _is_whitespace(char):
                output.append(" ")
            else:
                output.append(char)
        return "".join(output)


class WordpieceTokenizer(object):
    """Runs WordPiece tokenziation."""

    def __init__(self, vocab, unk_token="[UNK]", max_input_chars_per_word=400):
        self.vocab = vocab
        self.unk_token = unk_token
        self.max_input_chars_per_word = max_input_chars_per_word

    def tokenize(self, text):
        """Tokenizes a piece of text into its word pieces.

        This uses a greedy longest-match-first algorithm to perform tokenization
        using the given vocabulary.

        For example:
          input = "unaffable"
          output = ["un", "##aff", "##able"]

        Args:
          text: A single token or whitespace separated tokens. This should have
            already been passed through `BasicTokenizer.

        Returns:
          A list of wordpiece tokens.
        """

        text = convert_to_unicode(text)

        output_tokens = []
        for token in whitespace_tokenize(text):
            chars = list(token)
            if len(chars) > self.max_input_chars_per_word:
                output_tokens.append(self.unk_token)  # pragma: no cover
                continue  # pragma: no cover

            is_bad = False
            start = 0
            sub_tokens = []
            while start < len(chars):
                end = len(chars)
                cur_substr = None
                while start < end:
                    substr = "".join(chars[start:end])
                    if start > 0:
                        substr = "##" + substr  # pragma: no cover
                    if substr in self.vocab:
                        cur_substr = substr
                        break
                    end -= 1  # pragma: no cover
                if cur_substr is None:
                    is_bad = True  # pragma: no cover
                    break  # pragma: no cover
                sub_tokens.append(cur_substr)
                start = end

            if is_bad:
                output_tokens.append(self.unk_token)  # pragma: no cover
            else:
                output_tokens.extend(sub_tokens)
        return output_tokens


def _is_control(char):
    """Checks whether `chars` is a control character."""
    # These are technically control characters but we count them as whitespace
    # characters.
    if char == "\t" or char == "\n" or char == "\r":
        return False  # pragma: no cover
    cat = unicodedata.category(char)
    if cat in ("Cc", "Cf"):
        return True  # pragma: no cover
    return False


def _is_whitespace(char):
    """Checks whether `chars` is a whitespace character."""
    # \t, \n, and \r are technically control characters but we treat them
    # as whitespace since they are generally considered as such.
    if char == " " or char == "\t" or char == "\n" or char == "\r":
        return True
    cat = unicodedata.category(char)
    if cat == "Zs":
        return True  # pragma: no cover
    return False


def whitespace_tokenize(text):
    """Runs basic whitespace cleaning and splitting on a piece of text."""
    text = text.strip()
    if not text:
        return []
    tokens = text.split()
    return tokens


def _is_punctuation(char):
    """Checks whether `chars` is a punctuation character."""
    cp = ord(char)
    # We treat all non-letter/number ASCII as punctuation.
    # Characters such as "^", "$", and "`" are not in the Unicode
    # Punctuation class but we treat them as punctuation anyways, for
    # consistency.
    if (
        (cp >= 33 and cp <= 47)
        or (cp >= 58 and cp <= 64)
        or (cp >= 91 and cp <= 96)
        or (cp >= 123 and cp <= 126)
    ):
        return True
    cat = unicodedata.category(char)
    if cat.startswith("P"):
        return True  # pragma: no cover
    return False


# class ObjectDetectionPreProcessing(preprocessor.Preprocessor):
@tf.keras.utils.register_keras_serializable()
class ObjectDetectionPreProcessing(object):
    """ObjectDetectionPreProcessing layer.

    # Arguments
        max_sequence_length: maximum length of the sequences after vectorization.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # self.tokenizer = tokenization.FullTokenizer(
        #     # vocab_file=constants.BERT_VOCAB_PATH,
        #     do_lower_case=True,
        # )
        # self.max_sequence_length = max_sequence_length

    def get_config(self):
        config = super().get_config()
        # config.update({"max_sequence_length": self.max_sequence_length})
        return config

    def build(self, input_shape):
        self.batch_size = input_shape

    def call(self, input_x, input_y):
        """Applies preprocessing step to a batch of images and bboxes and their
        labels.

        Arguments:
          inputs: A batch of [image, (bbox, label)].

        Returns:
          images: List of Resized and padded images with random horizontal
            flipping applied.
          bboxes: List of Bounding boxes with the shape `(num_objects, 4)`
           where each box is of the format `[x, y, width, height]`.
          class_ids: List of tensors representing the class id of the objects, having
            shape `(num_objects,)`.
        """
        # images = []
        # bboxes = []
        # class_ids = []
        # for i in range(inputs.shape[0]):
        #     image, bbox, class_id = self.data_transform(inputs[i])
        #     images.append(image)
        #     bboxes.append(bbox)
        #     class_ids.append(class_id)
        # # check if this needs to be converted to tf.Dataset or not
        # images = tf.stack(images)
        # bboxes = tf.stack(bboxes)
        # class_ids = tf.stack(class_ids)
        # return images, bboxes, class_ids
        print(
            "input to call: ",
            tf.shape(input_x),
            tf.shape(input_y[0]),
            tf.shape(input_y[1]),
        )
        return self.data_transform(input_x, input_y)

    def data_transform(self, sample_x, sample_y):
        print(
            "input to data_transform: ",
            tf.shape(sample_x),
            tf.shape(sample_y[0]),
            tf.shape(sample_y[1]),
        )
        image = sample_x
        bbox = self.swap_xy(sample_y[0])  # check this function
        class_id = tf.cast(sample_y[1], dtype=tf.int32)

        image, bbox = self.random_flip_horizontal(image, bbox)
        image, image_shape, _ = self.resize_and_pad_image(image)

        bbox = tf.stack(
            [
                bbox[:, 0] * image_shape[1],
                bbox[:, 1] * image_shape[0],
                bbox[:, 2] * image_shape[1],
                bbox[:, 3] * image_shape[0],
            ],
            axis=-1,
        )
        bbox = self.convert_to_xywh(bbox)
        return image, bbox, class_id

    def random_flip_horizontal(self, image, boxes):
        """Flips image and boxes horizontally with 50% chance

        Arguments:
          image: A 3-D tensor of shape `(height, width, channels)` representing an
            image.
          boxes: A tensor with shape `(num_boxes, 4)` representing bounding boxes,
            having normalized coordinates.

        Returns:
          Randomly flipped image and boxes
        """
        if tf.random.uniform(()) > 0.5:
            image = tf.image.flip_left_right(image)
            boxes = tf.stack(
                [1 - boxes[:, 2], boxes[:, 1], 1 - boxes[:, 0], boxes[:, 3]], axis=-1
            )
        return image, boxes

    def resize_and_pad_image(
        self,
        image,
        min_side=800.0,
        max_side=1333.0,
        jitter=[640, 1024],
        stride=128.0,
    ):
        """Resizes and pads image while preserving aspect ratio.

        1. Resizes images so that the shorter side is equal to `min_side`
        2. If the longer side is greater than `max_side`, then resize the image
          with longer side equal to `max_side`
        3. Pad with zeros on right and bottom to make the image shape divisible by
        `stride`

        Arguments:
          image: A 3-D tensor of shape `(height, width, channels)` representing an
            image.
          min_side: The shorter side of the image is resized to this value, if
            `jitter` is set to None.
          max_side: If the longer side of the image exceeds this value after
            resizing, the image is resized such that the longer side now equals to
            this value.
          jitter: A list of floats containing minimum and maximum size for scale
            jittering. If available, the shorter side of the image will be
            resized to a random value in this range.
          stride: The stride of the smallest feature map in the feature pyramid.
            Can be calculated using `image_size / feature_map_size`.

        Returns:
          image: Resized and padded image.
          image_shape: Shape of the image before padding.
          ratio: The scaling factor used to resize the image
        """
        image_shape = tf.cast(tf.shape(image)[:2], dtype=tf.float32)
        if jitter is not None:
            min_side = tf.random.uniform((), jitter[0], jitter[1], dtype=tf.float32)
        ratio = min_side / tf.reduce_min(image_shape)
        if ratio * tf.reduce_max(image_shape) > max_side:
            ratio = max_side / tf.reduce_max(image_shape)
        image_shape = ratio * image_shape
        image = tf.image.resize(image, tf.cast(image_shape, dtype=tf.int32))
        padded_image_shape = tf.cast(
            tf.math.ceil(image_shape / stride) * stride, dtype=tf.int32
        )
        image = tf.image.pad_to_bounding_box(
            image, 0, 0, padded_image_shape[0], padded_image_shape[1]
        )
        return image, image_shape, ratio

    def swap_xy(self, boxes):
        """Swaps order the of x and y coordinates of the boxes.

        Arguments:
          boxes: A tensor with shape `(num_boxes, 4)` representing bounding boxes.

        Returns:
          swapped boxes with shape same as that of boxes.
        """
        print("input to swapy_xy: ", tf.shape(boxes[0]))
        return tf.stack(
            [boxes[:, 1], boxes[:, 0], boxes[:, 3], boxes[:, 2]], axis=-1
        )

    def convert_to_xywh(self, boxes):
        """Changes the box format to center, width and height.

        Arguments:
          boxes: A tensor of rank 2 or higher with a shape of `(..., num_boxes, 4)`
            representing bounding boxes where each box is of the format
            `[xmin, ymin, xmax, ymax]`.

        Returns:
          converted boxes with shape same as that of boxes.
        """
        return tf.concat(
            [
                (boxes[..., :2] + boxes[..., 2:]) / 2.0,
                boxes[..., 2:] - boxes[..., :2],
            ],
            axis=-1,
        )


@tf.keras.utils.register_keras_serializable()
class FeaturePyramid(tf.keras.layers.Layer):
    """Builds the Feature Pyramid with the feature maps from the backbone.

    Attributes:
      num_classes: Number of classes in the dataset.
      backbone: The backbone to build the feature pyramid from.
        Currently supports ResNet50 only.
    """

    def __init__(self, backbone=None, **kwargs):
        super(FeaturePyramid, self).__init__(name="FeaturePyramid", **kwargs)
        self.backbone = backbone if backbone else self.get_backbone()
        self.conv_c3_1x1 = tf.keras.layers.Conv2D(256, 1, 1, "same")
        self.conv_c4_1x1 = tf.keras.layers.Conv2D(256, 1, 1, "same")
        self.conv_c5_1x1 = tf.keras.layers.Conv2D(256, 1, 1, "same")
        self.conv_c3_3x3 = tf.keras.layers.Conv2D(256, 3, 1, "same")
        self.conv_c4_3x3 = tf.keras.layers.Conv2D(256, 3, 1, "same")
        self.conv_c5_3x3 = tf.keras.layers.Conv2D(256, 3, 1, "same")
        self.conv_c6_3x3 = tf.keras.layers.Conv2D(256, 3, 2, "same")
        self.conv_c7_3x3 = tf.keras.layers.Conv2D(256, 3, 2, "same")
        self.upsample_2x = tf.keras.layers.UpSampling2D(2)

    def get_config(self):
        config = super().get_config()
        config.update({"backbone": self.backbone})
        return config

    def from_config(cls, config):
        return cls(**config)

    def call(self, images, training=False):
        c3_output, c4_output, c5_output = self.backbone(images, training=training)
        p3_output = self.conv_c3_1x1(c3_output)
        p4_output = self.conv_c4_1x1(c4_output)
        p5_output = self.conv_c5_1x1(c5_output)
        p4_output = p4_output + self.upsample_2x(p5_output)
        p3_output = p3_output + self.upsample_2x(p4_output)
        p3_output = self.conv_c3_3x3(p3_output)
        p4_output = self.conv_c4_3x3(p4_output)
        p5_output = self.conv_c5_3x3(p5_output)
        p6_output = self.conv_c6_3x3(c5_output)
        p7_output = self.conv_c7_3x3(tf.nn.relu(p6_output))
        return p3_output, p4_output, p5_output, p6_output, p7_output

    def get_backbone(self):
        """Builds ResNet50 with pre-trained imagenet weights"""
        backbone = tf.keras.applications.ResNet50(
            include_top=False, input_shape=[None, None, 3]
        )
        c3_output, c4_output, c5_output = [
            backbone.get_layer(layer_name).output
            for layer_name in [
                "conv3_block4_out",
                "conv4_block6_out",
                "conv5_block3_out",
            ]
        ]
        return tf.keras.Model(
            inputs=[backbone.inputs], outputs=[c3_output, c4_output, c5_output]
        )


@tf.keras.utils.register_keras_serializable()
class AnchorBox:
    """Generates anchor boxes.

    This class has operations to generate anchor boxes for feature maps at
    strides `[8, 16, 32, 64, 128]`. Where each anchor each box is of the
    format `[x, y, width, height]`.

    Attributes:
      aspect_ratios: A list of float values representing the aspect ratios of
        the anchor boxes at each location on the feature map
      scales: A list of float values representing the scale of the anchor boxes
        at each location on the feature map.
      num_anchors: The number of anchor boxes at each location on feature map
      areas: A list of float values representing the areas of the anchor
        boxes for each feature map in the feature pyramid.
      strides: A list of float value representing the strides for each feature
        map in the feature pyramid.
    """

    def __init__(self):
        self.aspect_ratios = [0.5, 1.0, 2.0]
        self.scales = [2 ** x for x in [0, 1 / 3, 2 / 3]]

        self._num_anchors = len(self.aspect_ratios) * len(self.scales)
        self._strides = [2 ** i for i in range(3, 8)]
        self._areas = [x ** 2 for x in [32.0, 64.0, 128.0, 256.0, 512.0]]
        self._anchor_dims = self._compute_dims()

    def get_config(self):
        # config  = {
        #     "aspect_ratios": self.aspect_ratios,
        #     "scales": self.scales,
        #     "_num_anchors": self._num_anchors,
        #     "_strides": self._strides,
        #     "_areas": self._areas,
        #     "_anchor_dims": self._anchor_dims
        # }
        # return config
        return {}

    def from_config(cls, config):
        """Build an instance from the config of this object.

        # Arguments
            config: Dict. The config of the object.
        """
        return cls(**config)

    def _compute_dims(self):
        """Computes anchor box dimensions for all ratios and scales at all levels
        of the feature pyramid.
        """
        anchor_dims_all = []
        for area in self._areas:
            anchor_dims = []
            for ratio in self.aspect_ratios:
                anchor_height = tf.math.sqrt(area / ratio)
                anchor_width = area / anchor_height
                dims = tf.reshape(
                    tf.stack([anchor_width, anchor_height], axis=-1), [1, 1, 2]
                )
                for scale in self.scales:
                    anchor_dims.append(scale * dims)
            anchor_dims_all.append(tf.stack(anchor_dims, axis=-2))
        return anchor_dims_all

    def _get_anchors(self, feature_height, feature_width, level):
        """Generates anchor boxes for a given feature map size and level

        Arguments:
          feature_height: An integer representing the height of the feature map.
          feature_width: An integer representing the width of the feature map.
          level: An integer representing the level of the feature map in the
            feature pyramid.

        Returns:
          anchor boxes with the shape
          `(feature_height * feature_width * num_anchors, 4)`
        """
        rx = tf.range(feature_width, dtype=tf.float32) + 0.5
        ry = tf.range(feature_height, dtype=tf.float32) + 0.5
        centers = tf.stack(tf.meshgrid(rx, ry), axis=-1) * self._strides[level - 3]
        centers = tf.expand_dims(centers, axis=-2)
        centers = tf.tile(centers, [1, 1, self._num_anchors, 1])
        dims = tf.tile(
            self._anchor_dims[level - 3], [feature_height, feature_width, 1, 1]
        )
        anchors = tf.concat([centers, dims], axis=-1)
        return tf.reshape(
            anchors, [feature_height * feature_width * self._num_anchors, 4]
        )

    def get_anchors(self, image_height, image_width):
        """Generates anchor boxes for all the feature maps of the feature pyramid.

        Arguments:
          image_height: Height of the input image.
          image_width: Width of the input image.

        Returns:
          anchor boxes for all the feature maps, stacked as a single tensor
            with shape `(total_anchors, 4)`
        """
        anchors = [
            self._get_anchors(
                tf.math.ceil(image_height / 2 ** i),
                tf.math.ceil(image_width / 2 ** i),
                i,
            )
            for i in range(3, 8)
        ]
        return tf.concat(anchors, axis=0)


@tf.keras.utils.register_keras_serializable()
class RetinaNet(tf.keras.Model):
    """A subclassed Keras model implementing the RetinaNet architecture.

    Attributes:
      num_classes: Number of classes in the dataset.
      backbone: The backbone to build the feature pyramid from.
        Currently supports ResNet50 only.
    """

    def __init__(self, num_classes, backbone=None, **kwargs):
        super(RetinaNet, self).__init__(name="RetinaNet", **kwargs)
        self.fpn = FeaturePyramid(backbone)
        self.num_classes = num_classes

        # prior_probability = tf.constant_initializer(-np.log((1 - 0.01) / 0.01))
        # self.cls_head = build_head(9 * num_classes, prior_probability)
        # self.box_head = build_head(9 * 4, "zeros")

    def get_config(self):
        config = super().get_config()
        config.update({"num_classes": self.num_classes, "backbone": self.backbone})
        return config

    def from_config(cls, config):
        return cls(**config)

    def call(self, image, training=False):
        features = self.fpn(image, training=training)
        # N = tf.shape(image)[0]
        # cls_outputs = []
        # box_outputs = []
        # for feature in features:
        #     box_outputs.append(tf.reshape(self.box_head(feature), [N, -1, 4]))
        #     cls_outputs.append(
        #         tf.reshape(self.cls_head(feature), [N, -1, self.num_classes])
        #     )
        # cls_outputs = tf.concat(cls_outputs, axis=1)
        # box_outputs = tf.concat(box_outputs, axis=1)
        # return tf.concat([box_outputs, cls_outputs], axis=-1)
        return features


@tf.keras.utils.register_keras_serializable()
def build_head(output_filters, bias_init):
    """Builds the class/box predictions head.

    Arguments:
      output_filters: Number of convolution filters in the final layer.
      bias_init: Bias Initializer for the final convolution layer.

    Returns:
      A keras sequential model representing either the classification
        or the box regression head depending on `output_filters`.
    """
    head = tf.keras.Sequential([tf.keras.Input(shape=[None, None, 256])])
    kernel_init = tf.initializers.RandomNormal(0.0, 0.01)
    for _ in range(4):
        head.add(
            tf.keras.layers.Conv2D(
                256, 3, padding="same", kernel_initializer=kernel_init
            )
        )
        head.add(tf.keras.layers.ReLU())
    head.add(
        tf.keras.layers.Conv2D(
            output_filters,
            3,
            1,
            padding="same",
            kernel_initializer=kernel_init,
            bias_initializer=bias_init,
        )
    )
    return head


@tf.keras.utils.register_keras_serializable()
class ObjectDetectionLabelEncoder(
    object
):  # change name, get_config and set_config, serialize and de-serialize
    """Transforms the raw labels into targets for training.

    This class has operations to generate targets for a batch of samples which
    is made up of the input images, bounding boxes for the objects present and
    their class ids.

    Attributes:
      anchor_box: Anchor box generator to encode the bounding boxes.
      box_variance: The scaling factors used to scale the bounding box targets.
    """

    def __init__(self):
        self._anchor_box = AnchorBox()
        self._box_variance = tf.convert_to_tensor(
            [0.1, 0.1, 0.2, 0.2], dtype=tf.float32
        )

    def get_config(self):
        return {"_anchor_box": self._anchor_box, "_box_variance": self._box_variance}

    def from_config(cls, config):
        """Build an instance from the config of this object.

        # Arguments
            config: Dict. The config of the object.
        """
        return cls(**config)

    def _match_anchor_boxes(
        self, anchor_boxes, gt_boxes, match_iou=0.5, ignore_iou=0.4
    ):
        """Matches ground truth boxes to anchor boxes based on IOU.

        1. Calculates the pairwise IOU for the M `anchor_boxes` and N `gt_boxes`
          to get a `(M, N)` shaped matrix.
        2. The ground truth box with the maximum IOU in each row is assigned to
          the anchor box provided the IOU is greater than `match_iou`.
        3. If the maximum IOU in a row is less than `ignore_iou`, the anchor
          box is assigned with the background class.
        4. The remaining anchor boxes that do not have any class assigned are
          ignored during training.

        Arguments:
          anchor_boxes: A float tensor with the shape `(total_anchors, 4)`
            representing all the anchor boxes for a given input image shape,
            where each anchor box is of the format `[x, y, width, height]`.
          gt_boxes: A float tensor with shape `(num_objects, 4)` representing
            the ground truth boxes, where each box is of the format
            `[x, y, width, height]`.
          match_iou: A float value representing the minimum IOU threshold for
            determining if a ground truth box can be assigned to an anchor box.
          ignore_iou: A float value representing the IOU threshold under which
            an anchor box is assigned to the background class.

        Returns:
          matched_gt_idx: Index of the matched object
          positive_mask: A mask for anchor boxes that have been assigned ground
            truth boxes.
          ignore_mask: A mask for anchor boxes that need to by ignored during
            training
        """
        iou_matrix = self.compute_iou(anchor_boxes, gt_boxes)
        max_iou = tf.reduce_max(iou_matrix, axis=1)
        matched_gt_idx = tf.argmax(iou_matrix, axis=1)
        positive_mask = tf.greater_equal(max_iou, match_iou)
        negative_mask = tf.less(max_iou, ignore_iou)
        ignore_mask = tf.logical_not(tf.logical_or(positive_mask, negative_mask))
        return (
            matched_gt_idx,
            tf.cast(positive_mask, dtype=tf.float32),
            tf.cast(ignore_mask, dtype=tf.float32),
        )

    def compute_iou(self, boxes1, boxes2):
        """Computes pairwise IOU matrix for given two sets of boxes

        Arguments:
          boxes1: A tensor with shape `(N, 4)` representing bounding boxes
            where each box is of the format `[x, y, width, height]`.
            boxes2: A tensor with shape `(M, 4)` representing bounding boxes
            where each box is of the format `[x, y, width, height]`.

        Returns:
          pairwise IOU matrix with shape `(N, M)`, where the value at ith row
            jth column holds the IOU between ith box and jth box from
            boxes1 and boxes2 respectively.
        """
        boxes1_corners = self.convert_to_corners(boxes1)
        boxes2_corners = self.convert_to_corners(boxes2)
        lu = tf.maximum(boxes1_corners[:, None, :2], boxes2_corners[:, :2])
        rd = tf.minimum(boxes1_corners[:, None, 2:], boxes2_corners[:, 2:])
        intersection = tf.maximum(0.0, rd - lu)
        intersection_area = intersection[:, :, 0] * intersection[:, :, 1]
        boxes1_area = boxes1[:, 2] * boxes1[:, 3]
        boxes2_area = boxes2[:, 2] * boxes2[:, 3]
        union_area = tf.maximum(
            boxes1_area[:, None] + boxes2_area - intersection_area, 1e-8
        )
        return tf.clip_by_value(intersection_area / union_area, 0.0, 1.0)

    def convert_to_corners(self, boxes):
        """Changes the box format to corner coordinates

        Arguments:
          boxes: A tensor of rank 2 or higher with a shape of `(..., num_boxes, 4)`
            representing bounding boxes where each box is of the format
            `[x, y, width, height]`.

        Returns:
          converted boxes with shape same as that of boxes.
        """
        return tf.concat(
            [
                boxes[..., :2] - boxes[..., 2:] / 2.0,
                boxes[..., :2] + boxes[..., 2:] / 2.0,
            ],
            axis=-1,
        )

    def _compute_box_target(self, anchor_boxes, matched_gt_boxes):
        """Transforms the ground truth boxes into targets for training"""
        box_target = tf.concat(
            [
                (matched_gt_boxes[:, :2] - anchor_boxes[:, :2])
                / anchor_boxes[:, 2:],
                tf.math.log(matched_gt_boxes[:, 2:] / anchor_boxes[:, 2:]),
            ],
            axis=-1,
        )
        box_target = box_target / self._box_variance
        return box_target

    def _encode_sample(self, image_shape, gt_boxes, cls_ids):
        """Creates box and classification targets for a single sample"""
        anchor_boxes = self._anchor_box.get_anchors(image_shape[1], image_shape[2])
        cls_ids = tf.cast(cls_ids, dtype=tf.float32)
        matched_gt_idx, positive_mask, ignore_mask = self._match_anchor_boxes(
            anchor_boxes, gt_boxes
        )
        matched_gt_boxes = tf.gather(gt_boxes, matched_gt_idx)
        box_target = self._compute_box_target(anchor_boxes, matched_gt_boxes)
        matched_gt_cls_ids = tf.gather(cls_ids, matched_gt_idx)
        cls_target = tf.where(
            tf.not_equal(positive_mask, 1.0), -1.0, matched_gt_cls_ids
        )
        cls_target = tf.where(tf.equal(ignore_mask, 1.0), -2.0, cls_target)
        cls_target = tf.expand_dims(cls_target, axis=-1)
        label = tf.concat([box_target, cls_target], axis=-1)
        return label

    def encode_batch(self, batch_images, gt_boxes, cls_ids):
        """Creates box and classification targets for a batch
        Not required anymore.
        """
        images_shape = tf.shape(batch_images)
        batch_size = images_shape[0]

        labels = tf.TensorArray(dtype=tf.float32, size=batch_size, dynamic_size=True)
        for i in range(batch_size):
            label = self._encode_sample(images_shape, gt_boxes[i], cls_ids[i])
            labels = labels.write(i, label)
        batch_images = tf.keras.applications.resnet.preprocess_input(
            batch_images
        )  # TODO
        return batch_images, labels.stack()

    def encode_sample_func(self, image, gt_boxes, cls_ids):
        image_shape = [0] + tf.shape(image)
        label = self._encode_sample(image_shape, gt_boxes, cls_ids)
        preprocessed_image = tf.keras.applications.resnet.preprocess_input(image)
        return preprocessed_image, label


@tf.keras.utils.register_keras_serializable()
class RetinaNetBoxLoss(tf.losses.Loss):
    """Implements Smooth L1 loss"""

    def __init__(self, delta):
        super(RetinaNetBoxLoss, self).__init__(
            reduction="none", name="RetinaNetBoxLoss"
        )
        self._delta = delta

    def call(self, y_true, y_pred):
        difference = y_true - y_pred
        absolute_difference = tf.abs(difference)
        squared_difference = difference ** 2
        loss = tf.where(
            tf.less(absolute_difference, self._delta),
            0.5 * squared_difference,
            absolute_difference - 0.5,
        )
        return tf.reduce_sum(loss, axis=-1)

    def get_config(self):
        config = super().get_config()
        config.update({"_delta": self._delta})
        return config

    def from_config(cls, config):
        return cls(**config)


@tf.keras.utils.register_keras_serializable()
class RetinaNetClassificationLoss(tf.losses.Loss):
    """Implements Focal loss"""

    def __init__(self, alpha, gamma):
        super(RetinaNetClassificationLoss, self).__init__(
            reduction="none", name="RetinaNetClassificationLoss"
        )
        self._alpha = alpha
        self._gamma = gamma

    def call(self, y_true, y_pred):
        cross_entropy = tf.nn.sigmoid_cross_entropy_with_logits(
            labels=y_true, logits=y_pred
        )
        probs = tf.nn.sigmoid(y_pred)
        alpha = tf.where(tf.equal(y_true, 1.0), self._alpha, (1.0 - self._alpha))
        pt = tf.where(tf.equal(y_true, 1.0), probs, 1 - probs)
        loss = alpha * tf.pow(1.0 - pt, self._gamma) * cross_entropy
        return tf.reduce_sum(loss, axis=-1)

    def get_config(self):
        config = super().get_config()
        config.update({"_alpha": self._alpha, "_gamma": self._gamma})
        return config

    def from_config(cls, config):
        return cls(**config)


@tf.keras.utils.register_keras_serializable()
class RetinaNetLoss(tf.losses.Loss):
    """Wrapper to combine both the losses"""

    def __init__(self, num_classes=80, alpha=0.25, gamma=2.0, delta=1.0):
        super(RetinaNetLoss, self).__init__(reduction="auto", name="RetinaNetLoss")
        self._clf_loss = RetinaNetClassificationLoss(alpha, gamma)
        self._box_loss = RetinaNetBoxLoss(delta)
        self._num_classes = num_classes

    def call(self, y_true, y_pred):
        y_pred = tf.cast(y_pred, dtype=tf.float32)
        box_labels = y_true[:, :, :4]
        box_predictions = y_pred[:, :, :4]
        cls_labels = tf.one_hot(
            tf.cast(y_true[:, :, 4], dtype=tf.int32),
            depth=self._num_classes,
            dtype=tf.float32,
        )
        cls_predictions = y_pred[:, :, 4:]
        positive_mask = tf.cast(tf.greater(y_true[:, :, 4], -1.0), dtype=tf.float32)
        ignore_mask = tf.cast(tf.equal(y_true[:, :, 4], -2.0), dtype=tf.float32)
        clf_loss = self._clf_loss(cls_labels, cls_predictions)
        box_loss = self._box_loss(box_labels, box_predictions)
        clf_loss = tf.where(tf.equal(ignore_mask, 1.0), 0.0, clf_loss)
        box_loss = tf.where(tf.equal(positive_mask, 1.0), box_loss, 0.0)
        normalizer = tf.reduce_sum(positive_mask, axis=-1)
        clf_loss = tf.math.divide_no_nan(
            tf.reduce_sum(clf_loss, axis=-1), normalizer
        )
        box_loss = tf.math.divide_no_nan(
            tf.reduce_sum(box_loss, axis=-1), normalizer
        )
        loss = clf_loss + box_loss
        return loss

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "_clf_loss": self._clf_loss,
                "_box_loss": self._box_loss,
                "_num_classes": self._num_classes,
            }
        )
        return config

    def from_config(cls, config):
        return cls(**config)
