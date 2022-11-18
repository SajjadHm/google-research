# coding=utf-8
# Copyright 2022 The Google Research Authors.
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

"""Tests for inference."""

import functools
import os
from typing import Tuple

from absl.testing import absltest
import jax
import jax.numpy as jnp
import numpy as np

 import resources
from scaling_transformer_inference_efficiency import checkpoint
from scaling_transformer_inference_efficiency import incremental
from scaling_transformer_inference_efficiency import inference
from scaling_transformer_inference_efficiency import layers_parallel
from scaling_transformer_inference_efficiency import weights

jax.config.update('jax_array', True)  # required for jax < 0.4.0

# pylint: disable = line-too-long
# PaLM correctness test relies on internal checkpoints, return None in external code


def golden_generation_unprompted():
  # Found by running this test :inference_test at a CL that has been manually
  # tested against PaLM-8B and was seen to produce meaningful English prose.
  return incremental.Chunk(
      np.array([[0, 5, 94, 19, 3, 9, 182, 514, 97, 12, 129, 3],
                [0, 3, 9, 3, 60, 4312, 8, 3, 60, 4312, 5, 3]], np.int32),
      np.array([12, 12], np.int32))


class InferenceTest(absltest.TestCase):

  def test_nonincremental_score(self):
    model, test_weights = load_toy_model()
    golden_chunk, golden_token_scores = get_golden()
    golden_chunk_result = model.prefill(test_weights, [],
                                        golden_chunk).copy_to_host()

    scores = np.where(golden_chunk.token_mask,
                      golden_chunk_result.per_token_scores, 0)
    np.testing.assert_allclose(scores, golden_token_scores, rtol=0.02)

  def test_incremental_score(self):
    model, test_weights = load_toy_model()
    golden_chunk, golden_token_scores = get_golden()

    for split in [3, 6]:
      a, b = golden_chunk.split_at(split)
      result_a = model.prefill(test_weights, [], a)
      result_b = model.prefill(test_weights, [result_a], b)
      scores = jnp.concatenate([
          result_a.copy_to_host().per_token_scores,
          result_b.copy_to_host().per_token_scores
      ],
                               axis=1)
      scores = np.where(golden_chunk.token_mask, scores, 0)
      np.testing.assert_allclose(scores, golden_token_scores, rtol=0.02)

  def test_unprompted_generation(self):
    model, test_weights = load_toy_model()
    num_samples = 2
    sample_ids = np.arange(num_samples)
    steps = 12
    temperature = 0.7
    samples, _ = model.generate(steps, test_weights,
                                incremental.Sampling(temperature), [],
                                sample_ids)
    np.testing.assert_array_equal(
        np.array(samples.tokens),
        golden_generation_unprompted().tokens)

  def test_unprompted_generation_incremental(self):
    model, test_weights = load_toy_model()
    num_samples = 2
    sample_ids = np.arange(num_samples)
    steps = 12
    temperature = 0.7
    for split in [1, 3]:
      samples_a, result_a = model.generate(split, test_weights,
                                           incremental.Sampling(temperature),
                                           [], sample_ids)
      samples_b, _ = model.generate(steps - split, test_weights,
                                    incremental.Sampling(temperature),
                                    [result_a], sample_ids)
      tokens = np.concatenate(
          [np.array(samples_a.tokens),
           np.array(samples_b.tokens)], axis=1)
      np.testing.assert_array_equal(tokens,
                                    golden_generation_unprompted().tokens)

  def test_prompted_generation_two_stages(self):
    model, test_weights = load_toy_model()
    num_samples = 2
    sample_ids = np.arange(num_samples)
    temperature = 0.7

    golden_chunk = golden_generation_unprompted()
    # We'll prompt with the first 8 tokens (split into two chunks), and
    # regenerate the last 4 tokens.
    chunk_a, chunk_bc = golden_chunk.split_at(4)
    chunk_b, chunk_c = chunk_bc.split_at(4)
    # Additionally, we'll pad chunk_a, to test padding effects.
    chunk_a = chunk_a.pad_to_length(6)

    result_a = model.prefill(test_weights, [], chunk_a)
    result_b = model.prefill(test_weights, [result_a], chunk_b)
    samples, _ = model.generate(chunk_c.tokens.shape[1], test_weights,
                                incremental.Sampling(temperature),
                                [result_a, result_b], sample_ids)

    np.testing.assert_array_equal(np.array(samples.tokens), chunk_c.tokens)

if __name__ == '__main__':
  absltest.main()
