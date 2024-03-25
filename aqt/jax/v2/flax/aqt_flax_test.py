# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test for AQT flax."""

import functools

from absl.testing import absltest
from absl.testing import parameterized
from aqt.jax.v2 import config
from aqt.jax.v2.flax import aqt_flax
import flax.linen as nn
import jax
import jax.numpy as jnp
import numpy as np


class AqtFlaxTest(parameterized.TestCase):

  def test_aqt_einsum(self):
    class Model(nn.Module):
      aqt_cfg: config.DotGeneral | None
      lhs_qt_external: bool = False
      rhs_qt_external: bool = False

      @nn.compact
      def __call__(self, lhs, rhs):
        lhs_in, rhs_in = lhs, rhs
        assert not (
            (self.lhs_qt_external or self.rhs_qt_external)
            and (self.aqt_cfg is None)
        ), 'aqt_cfg cannot be None when providing qtensor as inputs to einsum'
        if self.lhs_qt_external or self.rhs_qt_external:
          (lhs_q, _), (rhs_q, _) = self.aqt_cfg.fwd.dg_quantizer(
              (lhs, (2, 3)), (rhs, (1, 2))
          )

          lhs_dtype = self.aqt_cfg.fwd.dg_quantizer.lhs.numerics.get_dtype()
          rhs_dtype = self.aqt_cfg.fwd.dg_quantizer.rhs.numerics.get_dtype()
          if self.lhs_qt_external:
            lhs_in = lhs_q.qvalue_astype(lhs_dtype)
          if self.rhs_qt_external:
            rhs_in = rhs_q.qvalue_astype(rhs_dtype)

        einsum = aqt_flax.AqtEinsum(cfg=self.aqt_cfg)
        # xhs_qt can be inputs to AqtEinsum
        # xhs->xhs_qt can happen outside of AqtEinsum, e.g., k/v cache quant
        # input xhs_qt will force get_tensor() to always return xhs_qt
        out = einsum('ijkh,mkh->ijm', lhs_in, rhs_in)
        return out

    key = jax.random.PRNGKey(0)
    subkey1, subkey2 = jax.random.split(key, num=2)
    lhs = jax.random.uniform(subkey1, shape=(3, 4, 5, 6))
    rhs = jax.random.uniform(subkey2, shape=(2, 5, 6))

    def test(model_cls, cfg):
      model = model_cls(cfg)
      out = model.apply({}, lhs, rhs, rngs={'params': jax.random.PRNGKey(0)})
      # print(f'{out.shape=}')
      # print(f'{out=}')
      return out

    out_float = test(Model, None)
    out_int8 = test(Model, config.config_v4())
    out_int8_lqt = test(
        functools.partial(Model, lhs_qt_external=True), config.config_v4()
    )
    out_int8_rqt = test(
        functools.partial(Model, rhs_qt_external=True), config.config_v4()
    )
    out_int8_qt = test(
        functools.partial(Model, lhs_qt_external=True, rhs_qt_external=True),
        config.config_v4(),
    )

    assert (out_int8 == out_int8_lqt).all(), 'lhs external qt failed'
    assert (out_int8 == out_int8_rqt).all(), 'rhs external qt failed'
    assert (out_int8 == out_int8_qt).all(), 'both external qt failed'
    mse = jnp.mean(jnp.square(out_int8_qt - out_float))
    assert mse > 0, 'Mean square error is 0. Einsum is not quantized.'

  def test_einsum_grad_leak(self):
    class CNN(nn.Module):
      aqt_cfg: config.DotGeneral

      @nn.compact
      def __call__(self, x):
        einsum = aqt_flax.AqtEinsum(self.aqt_cfg)
        x = einsum('bc,ab->ac', jnp.identity(10, dtype=x.dtype), x)
        return x

    model = CNN(aqt_cfg=config.fully_quantized())
    var = model.init({'params': jax.random.PRNGKey(0)}, jnp.ones(shape=(1, 10)))

    @jax.jit
    def apply_fn(inputs):
      return model.apply(
          var, inputs, rngs={'params': jax.random.PRNGKey(0)}, mutable=True
      )

    @jax.jit
    @jax.value_and_grad
    def train_step(inputs):
      return jnp.sum(apply_fn(inputs)[0])

    train_step(jnp.ones(shape=(1, 10)))

  @parameterized.parameters(True, False)
  def test_freezer(self, use_legacy_freezer: bool):
    class Model(nn.Module):
      aqt_cfg: config.DotGeneral | None
      lhs_quant_mode: aqt_flax.QuantMode
      rhs_quant_mode: aqt_flax.QuantMode
      use_legacy_freezer: bool

      @nn.compact
      def __call__(self, lhs):
        kernel = self.param(
            'kernel', nn.initializers.lecun_normal(), shape=(2, 5, 6)
        )
        einsum = aqt_flax.AqtEinsum(
            cfg=self.aqt_cfg,
            lhs_quant_mode=self.lhs_quant_mode,
            rhs_quant_mode=self.rhs_quant_mode,
            use_legacy_freezer=self.use_legacy_freezer,
        )
        out = einsum('ijkh,mkh->ijm', lhs, kernel)
        return out

    key = jax.random.PRNGKey(0)
    subkeys = jax.random.split(key, num=6)
    lhs = jax.random.uniform(subkeys[0], shape=(3, 4, 5, 6))

    aqt_cfg = config.config_v4()
    train_model = Model(
        aqt_cfg,
        lhs_quant_mode=aqt_flax.QuantMode.TRAIN,
        rhs_quant_mode=aqt_flax.QuantMode.TRAIN,
        use_legacy_freezer=use_legacy_freezer,
    )

    # Consider the initialized params as the already-trained params,
    # since we are testing Freezers which do nothing during training.
    train_model_params = train_model.init(subkeys[1], lhs)
    inference_result = train_model.apply(
        train_model_params, lhs, rngs={'params': subkeys[2]}
    )

    # Convert test.
    convert_model = Model(
        aqt_cfg,
        lhs_quant_mode=aqt_flax.QuantMode.TRAIN,
        rhs_quant_mode=aqt_flax.QuantMode.CONVERT,
        use_legacy_freezer=use_legacy_freezer,
    )
    _, converted_params = convert_model.apply(
        train_model_params, lhs, mutable=True, rngs={'params': subkeys[3]}
    )

    aqt_params = converted_params['aqt']
    w = converted_params['params']['kernel']
    w_qtensor = aqt_params['AqtEinsum_0']['AqtDotGeneral_0']['qrhs']
    if use_legacy_freezer:
      w_quant = w_qtensor['value']
    else:
      w_quant = w_qtensor['frozen'].qvalue

    self.assertEqual(w.shape, w_quant.shape)
    self.assertEqual(w_quant.dtype, jnp.int8)

    # Serve test.
    serve_model = Model(
        aqt_cfg,
        lhs_quant_mode=aqt_flax.QuantMode.TRAIN,
        rhs_quant_mode=aqt_flax.QuantMode.SERVE,
        use_legacy_freezer=use_legacy_freezer,
    )
    serve_model_init = serve_model.init(subkeys[4], lhs)

    # See if the QTensors are initialized properly to handle the general serving
    # pipeline of: initialize --> replace values with loaded checkpoint --> run
    init_aqt_params = serve_model_init['aqt']
    init_qtensor = init_aqt_params['AqtEinsum_0']['AqtDotGeneral_0']['qrhs']

    init_leaves, init_treedef = jax.tree_flatten(init_qtensor)
    converted_leaves, converted_treedef = jax.tree_flatten(w_qtensor)

    # 1. Same treestructure.
    self.assertEqual(init_treedef, converted_treedef)

    # 2. Leaves with the same shapes and dtypes. Values could be different.
    self.assertEqual(len(init_leaves), len(converted_leaves))
    for init_leaf, converted_leaf in zip(init_leaves, converted_leaves):
      self.assertEqual(init_leaf.shape, converted_leaf.shape)
      self.assertEqual(init_leaf.dtype, converted_leaf.dtype)

    # Serving test with converted params.
    quantized_result = serve_model.apply(
        converted_params, lhs, rngs={'params': subkeys[5]}
    )

    np.testing.assert_allclose(inference_result, quantized_result)


if __name__ == '__main__':
  absltest.main()
