r"""PSPNet Semantic Segmentation architecture.

As described in http://arxiv.org/abs/1612.01105.

  Pyramid Scene Parsing Network
  Hengshuang Zhao, Jianping Shi, Xiaojuan Qi, Xiaoyong Shen, Jiaya Jia

This is a baseline architecture that was implemented for the purposes
of validating the other segmentation models which are more efficient.

Please note that although this network is accurate, it is VERY
slow and memory intensive. It should not be used under the assumption that
it will result in similar performance as the other models in this project.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from abc import abstractmethod

import tensorflow as tf

from libs import base_model as model
from libs import compressible_ops as ops

slim = tf.contrib.slim


class PSPNetArchitecture(model.FastSegmentationModel):
    """PSPNet Architecture definition."""

    def __init__(self,
                 is_training,
                 model_arg_scope,
                 num_classes,
                 feature_extractor,
                 classification_loss,
                 filter_scale,
                 pooling_factors,
                 use_aux_loss=True,
                 main_loss_weight=1,
                 aux_loss_weight=0,
                 upsample_train_logits=False,
                 add_summaries=True,
                 scope=None):
        super(PSPNetArchitecture, self).__init__(num_classes=num_classes)
        self._is_training = is_training
        self._model_arg_scope = model_arg_scope
        self._num_classes = num_classes
        self._feature_extractor = feature_extractor
        self._classification_loss = classification_loss
        self._filter_scale = filter_scale
        self._pooling_factors = pooling_factors
        self._use_aux_loss = use_aux_loss
        self._main_loss_weight = main_loss_weight
        self._aux_loss_weight = aux_loss_weight
        self._add_summaries = add_summaries
        self._upsample_train_logits = upsample_train_logits
        self._output_zoom_factor = 8.0
        self._scope = scope

    @property
    def main_class_predictions_key(self):
        return 'class_predictions'

    @property
    def aux_predictions_key(self):
        return 'aux_predictions'

    @property
    def main_loss_key(self):
        return 'loss'

    @property
    def aux_loss_key(self):
        return 'aux_loss'

    def preprocess(self, inputs):
        if inputs.dtype is not tf.float32:
            raise ValueError('`preprocess` expects a tf.float32 tensor')
        with tf.name_scope('Preprocessor'):
            return self._feature_extractor.preprocess(inputs)

    def _extract_shared_features(self, preprocessed_inputs, scope):
        return self._feature_extractor.extract_features(
            preprocessed_inputs, scope=scope)

    def predict(self, preprocessed_inputs):
        """Build main inference pass"""
        with slim.arg_scope(self._model_arg_scope):
            # Feature extraction from arbitrary extractor
            _, backbone_logits, psp_aux_out = self._extract_shared_features(
                preprocessed_inputs,
                scope=self.shared_feature_extractor_scope)
            # Branch specific layers
            final_logits = self._pspnet_pspmodule(backbone_logits)
            # Class class_predictions
            with tf.variable_scope('Predictions'):
                predictions = ops.conv2d(final_logits,
                                         self._num_classes, 1, 1,
                                         prediction_output=True)
                if not self._is_training:  # evaluation
                    predictions = self._dynamic_interpolation(
                        predictions, z_factor=self._output_zoom_factor)
            # Outputs with auxilarary loss for training
            prediction_dict = {
                self.main_class_predictions_key: predictions}
            # Aux loss as described in PSPNet paper
            if self._is_training and self._use_aux_loss:
                with tf.variable_scope('AuxPredictions'):
                    aux_preds = ops.conv2d(psp_aux_out,
                                           self._num_classes, 1, 1,
                                           prediction_output=True,
                                           scope='AuxOutput')
                prediction_dict[self.aux_predictions_key] = aux_preds
            return prediction_dict

    def _pspnet_pspmodule(self, input_features, scope=None):
        """PSP Module present in the original paper."""
        pooled_features = input_features
        branch_outputs = [input_features]
        input_shape = input_features.shape.as_list()
        input_h, input_w = input_shape[1], input_shape[2]

        output_pooling_shape = (input_h, input_w)
        with tf.variable_scope(scope, 'PSPModule'):
            for pooling_factor in self._pooling_factors:
                input_pooling_shape = (int(input_h / pooling_factor),
                                       int(input_w / pooling_factor))
                pooled_features = slim.avg_pool2d(
                    input_features,
                    input_pooling_shape,
                    stride=input_pooling_shape)
                # No filter are removed from these convs for simplicity.
                pooled_features = ops.conv2d(pooled_features, 512, 1, stride=1)
                pooled_features = tf.image.resize_bilinear(
                    pooled_features,
                    size=output_pooling_shape,
                    align_corners=True)
                branch_outputs.append(pooled_features)

            # Concat all branches
            branch_merge = tf.concat(branch_outputs, axis=-1)
            final_output = ops.conv2d(branch_merge, 512, 3,
                                      stride=1,
                                      compression_ratio=self._filter_scale)
        return final_output

    def _dynamic_interpolation(self, features_to_upsample,
                               s_factor=1.0, z_factor=1.0):
        with tf.name_scope('Interp'):
            features_shape = features_to_upsample.shape.as_list()
            input_h, input_w = features_shape[1], features_shape[2]
            shrink_h = (input_h - 1) * s_factor + 1
            shrink_w = (input_w - 1) * s_factor + 1
            zoom_h = shrink_h + (shrink_h - 1) * (z_factor - 1)
            zoom_w = shrink_w + (shrink_w - 1) * (z_factor - 1)
            return tf.image.resize_bilinear(features_to_upsample,
                                            size=[int(zoom_h), int(zoom_w)],
                                            align_corners=True)

    def loss(self, prediction_dict):
        losses_dict = {}

        # TODO: Make this an optional choice. For now only scale
        # down labels like in original paper
        def _resize_labels_to_logits(labels, logits):
            logits_shape = logits.get_shape().as_list()
            scaled_labels = tf.image.resize_nearest_neighbor(
                labels, logits_shape[1:3], align_corners=True)
            return scaled_labels

        main_preds = prediction_dict[self.main_class_predictions_key]
        with tf.name_scope('SegmentationLoss'):  # 1/8th labels
            if self._upsample_train_logits:
                main_preds = self._dynamic_interpolation(
                    main_preds, z_factor=self._output_zoom_factor)
            main_scaled_labels = _resize_labels_to_logits(
                self._groundtruth_labels, main_preds)
            main_loss = self._classification_loss(main_preds,
                                                  main_scaled_labels)
            losses_dict[self.main_loss_key] = (
                self._main_loss_weight * main_loss)

        if self._use_aux_loss and self._is_training:
            aux_preds = prediction_dict[self.aux_predictions_key]
            with tf.name_scope('AuxLoss'):  # 1/8th labels
                if self._upsample_train_logits:
                    aux_preds = self._dynamic_interpolation(
                        aux_preds, z_factor=self._output_zoom_factor)
                aux_scaled_labels = _resize_labels_to_logits(
                    self._groundtruth_labels, aux_preds)
                first_aux_loss = self._classification_loss(aux_preds,
                                                           aux_scaled_labels)
                losses_dict[self.aux_loss_key] = (
                    self._aux_loss_weight * first_aux_loss)
        return losses_dict

    def restore_map(self, fine_tune_checkpoint_type='classification'):
        """Restore variables for checkpoints correctly"""
        if fine_tune_checkpoint_type not in ['segmentation', 'classification']:
            raise ValueError('Not supported fine_tune_checkpoint_type: '
                             ' {}'.format(fine_tune_checkpoint_type))
        if fine_tune_checkpoint_type == 'classification':
            tf.logging.info('Fine-tuning from classification checkpoints.')
            return self._feature_extractor.restore_from_classif_checkpoint_fn(
                self.shared_feature_extractor_scope)
        exclude_list = ['global_step']
        variables_to_restore = slim.get_variables_to_restore(
            exclude=exclude_list)
        return variables_to_restore


class PSPNetFeatureExtractor(object):
    """PSPNet ICNet Based Feature Extractor definition."""

    def __init__(self,
                 is_training,
                 features_stride,
                 batch_norm_trainable=False,
                 reuse_weights=None,
                 weight_decay=0.0):
        self._is_training = is_training
        self._features_stride = features_stride
        self._train_batch_norm = (batch_norm_trainable and is_training)
        self._reuse_weights = reuse_weights
        self._weight_decay = weight_decay

    @abstractmethod
    def preprocess(self, raw_inputs):
        pass

    def extract_features(self, preprocessed_inputs, scope=None):
        """Extracts half resolution features."""
        with tf.variable_scope(scope,
                               values=[preprocessed_inputs],
                               reuse=tf.AUTO_REUSE):
            return self._extract_features(preprocessed_inputs, scope)

    @abstractmethod
    def _extract_features(self, preprocessed_inputs, scope):
        pass

    @staticmethod
    def restore_from_classif_checkpoint_fn(scope_name):
        variables_to_restore = {}
        for variable in tf.global_variables():
            if variable.op.name.startswith(scope_name):
                var_name = variable.op.name.replace(scope_name + '/', '')
                variables_to_restore[var_name] = variable
        return variables_to_restore
