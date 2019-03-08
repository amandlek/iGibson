# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
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


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from absl import app
from absl import flags
from absl import logging
import time
import os
import numpy as np

from gibson2.utils.tf_utils import env_load_fn, LayerParams

import tensorflow as tf
# from tf_agents.agents.ppo import ppo_agent
from gibson2.utils.agents.agents import ppo_agent
from tf_agents.drivers import dynamic_episode_driver
from tf_agents.environments import parallel_py_environment
from tf_agents.environments import tf_py_environment
from tf_agents.metrics import batched_py_metric
from tf_agents.metrics import metric_utils
from tf_agents.metrics import tf_metrics
from tf_agents.metrics.py_metrics import AverageEpisodeLengthMetric
from tf_agents.metrics.py_metrics import AverageReturnMetric
# from tf_agents.networks import actor_distribution_network
# from gibson2.utils.agents.networks import actor_distribution_network_original as actor_distribution_network
from gibson2.utils.agents.networks import actor_distribution_network
from tf_agents.networks import actor_distribution_rnn_network
# from tf_agents.networks import value_network
# from gibson2.utils.agents.networks import value_network_original as value_network
from gibson2.utils.agents.networks import value_network
from tf_agents.networks import value_rnn_network
from gibson2.utils.agents.networks import encoding_network
# from tf_agents.networks import encoding_network

# from tf_agents.policies import py_tf_policy
from gibson2.utils.agents.policies import py_tf_policy

# from tf_agents.replay_buffers import tf_uniform_replay_buffer
from gibson2.utils.agents.replay_buffers import tf_uniform_replay_buffer
from tf_agents.utils import common as common_utils

nest = tf.contrib.framework.nest

flags.DEFINE_string('root_dir', os.getenv('TEST_UNDECLARED_OUTPUTS_DIR'),
                    'Root directory for writing logs/summaries/checkpoints.')
flags.DEFINE_string('master', '', 'master session')
flags.DEFINE_integer('replay_buffer_capacity', 1001,
                     'Replay buffer capacity per env.')
flags.DEFINE_integer('batch_size', 64,
                     'Batch size for sampling from the replay buffer')
flags.DEFINE_integer('num_parallel_environments', 30,
                     'Number of environments to run in parallel')
flags.DEFINE_integer('num_environment_steps', 10000000,
                     'Number of environment steps to run before finishing.')
flags.DEFINE_integer('num_epochs', 25,
                     'Number of epochs for computing policy updates.')
flags.DEFINE_integer(
    'collect_episodes_per_iteration', 30,
    'The number of episodes to take in the environment before '
    'each update. This is the total across all parallel '
    'environments.')
flags.DEFINE_integer('num_eval_episodes', 30,
                     'The number of episodes to run eval on.')
flags.DEFINE_boolean('use_rnns', False,
                     'If true, use RNN for policy and value function.')

# Added for Gibson
flags.DEFINE_string('config_file', '../test/test.yaml',
                    'Config file for the experiment.')
flags.DEFINE_string('mode', 'headless',
                    'mode for the simulator (gui or headless)')
flags.DEFINE_float('physics_timestep', 1 / 40.0,
                   'physics timestep for the simulator')
flags.DEFINE_string('gpu_c', '0',
                    'gpu id for compute, e.g. Tensorflow.')
flags.DEFINE_string('gpu_g', '1',
                    'gpu id for graphics, e.g. Gibson.')
FLAGS = flags.FLAGS


def train_eval(
        root_dir,
        tf_master='',
        gpu='1',
        env_load_fn=None,
        env_mode='headless',
        random_seed=0,
        batch_size=64,
        # TODO(kbanoop): rename to policy_fc_layers.
        conv_layer_params=None,
        encoder_fc_layers=(128, 64),
        actor_fc_layers=(128, 64),
        value_fc_layers=(128, 64),
        use_rnns=False,
        # Params for collect
        num_environment_steps=10000000,
        collect_episodes_per_iteration=30,
        num_parallel_environments=30,
        replay_buffer_capacity=1001,  # Per-environment
        # Params for train
        num_epochs=25,
        learning_rate=1e-4,
        # Params for evalActorDistributionNetwork
        num_eval_episodes=30,
        eval_interval=500,
        # Params for summaries and logging
        train_checkpoint_interval=100,
        policy_checkpoint_interval=50,
        rb_checkpoint_interval=200,
        log_interval=50,
        summary_interval=50,
        summaries_flush_secs=1,
        debug_summaries=False,
        summarize_grads_and_vars=False,
        eval_metrics_callback=None):
    """A simple train and eval for PPO."""
    if root_dir is None:
        raise AttributeError('train_eval requires a root_dir.')

    root_dir = os.path.expanduser(root_dir)
    train_dir = os.path.join(root_dir, 'train')
    eval_dir = os.path.join(root_dir, 'eval')

    train_summary_writer = tf.contrib.summary.create_file_writer(
        train_dir, flush_millis=summaries_flush_secs * 1000)
    train_summary_writer.set_as_default()

    eval_summary_writer = tf.contrib.summary.create_file_writer(
        eval_dir, flush_millis=summaries_flush_secs * 1000)

    eval_metrics = [
        batched_py_metric.BatchedPyMetric(
            AverageReturnMetric,
            metric_args={'buffer_size': num_eval_episodes},
            batch_size=1),
        batched_py_metric.BatchedPyMetric(
            AverageEpisodeLengthMetric,
            metric_args={'buffer_size': num_eval_episodes},
            batch_size=1),
    ]
    eval_summary_writer_flush_op = eval_summary_writer.flush()

    with tf.contrib.summary.record_summaries_every_n_global_steps(
            summary_interval):

        tf.compat.v1.set_random_seed(random_seed)

        gpu = [int(gpu_id) for gpu_id in gpu.split(',')]
        gpu_ids = np.linspace(0, len(gpu), num=num_parallel_environments + 1, dtype=np.int, endpoint=False)
        eval_py_env = parallel_py_environment.ParallelPyEnvironment(
            [lambda gpu_id=gpu[gpu_ids[0]]: env_load_fn('headless', gpu_id)])
        tf_py_env = [lambda gpu_id=gpu[gpu_ids[1]]: env_load_fn(env_mode, gpu_id)]
        tf_py_env += [lambda gpu_id=gpu[gpu_ids[env_id]]: env_load_fn('headless', gpu_id)
                      for env_id in range(2, num_parallel_environments + 1)]
        tf_env = tf_py_environment.TFPyEnvironment(
            parallel_py_environment.ParallelPyEnvironment(tf_py_env))

        optimizer = tf.compat.v1.train.AdamOptimizer(learning_rate=learning_rate)

        base_network = None
        preprocessing_layers_params = {
            'sensor': LayerParams(base_network=None, conv=None, fc=encoder_fc_layers),
            'rgb': LayerParams(base_network=None, conv=conv_layer_params, fc=None),
            'depth': LayerParams(base_network=None, conv=conv_layer_params, fc=None),
        }
        preprocessing_combiner_type = 'concat'

        actor_encoder = encoding_network.EncodingNetwork(
            tf_env.observation_spec(),
            base_network=base_network,
            preprocessing_layers_params=preprocessing_layers_params,
            preprocessing_combiner_type=preprocessing_combiner_type,
            kernel_initializer=tf.compat.v1.keras.initializers.glorot_uniform()
        )
        value_encoder = encoding_network.EncodingNetwork(
            tf_env.observation_spec(),
            base_network=base_network,
            preprocessing_layers_params=preprocessing_layers_params,
            preprocessing_combiner_type=preprocessing_combiner_type,
            kernel_initializer=tf.compat.v1.keras.initializers.glorot_uniform()
        )

        if use_rnns:
            actor_net = actor_distribution_rnn_network.ActorDistributionRnnNetwork(
                tf_env.observation_spec(),
                tf_env.action_spec(),
                input_fc_layer_params=actor_fc_layers,
                output_fc_layer_params=None)
            value_net = value_rnn_network.ValueRnnNetwork(
                tf_env.observation_spec(),
                input_fc_layer_params=value_fc_layers,
                output_fc_layer_params=None)
        else:
            actor_net = actor_distribution_network.ActorDistributionNetwork(
                tf_env.observation_spec(),
                tf_env.action_spec(),
                encoder=actor_encoder,
                fc_layer_params=actor_fc_layers,
                kernel_initializer=tf.compat.v1.keras.initializers.glorot_uniform(),
            )
            value_net = value_network.ValueNetwork(
                tf_env.observation_spec(),
                encoder=value_encoder,
                fc_layer_params=value_fc_layers,
                kernel_initializer=tf.compat.v1.keras.initializers.glorot_uniform()
            )

        tf_agent = ppo_agent.PPOAgent(
            tf_env.time_step_spec(),
            tf_env.action_spec(),
            optimizer,
            actor_net=actor_net,
            value_net=value_net,
            num_epochs=num_epochs,
            debug_summaries=debug_summaries,
            summarize_grads_and_vars=summarize_grads_and_vars,
            normalize_observations=True)

        replay_buffer = tf_uniform_replay_buffer.TFUniformReplayBuffer(
            tf_agent.collect_data_spec(),
            batch_size=num_parallel_environments,
            max_length=replay_buffer_capacity)

        valid_range_op = replay_buffer.valid_range_ids()

        # dataset = replay_buffer.as_dataset(
        #     num_parallel_calls=4,
        #     sample_batch_size=batch_size,
        #     num_steps=2).prefetch(4)
        # iterator = tf.compat.v1.data.make_initializable_iterator(dataset)

        eval_py_policy = py_tf_policy.PyTFPolicy(tf_agent.policy())

        # TODO(sguada): Reenable metrics when ready for batch data.
        environment_steps_metric = tf_metrics.EnvironmentSteps()
        environment_steps_count = environment_steps_metric.result()

        step_metrics = [
            tf_metrics.NumberOfEpisodes(),
            environment_steps_metric,
        ]
        train_metrics = step_metrics + [
            tf_metrics.AverageReturnMetric(),
            tf_metrics.AverageEpisodeLengthMetric(),
        ]

        # Add to replay buffer and other agent specific observers.
        replay_buffer_observer = [replay_buffer.add_batch]

        global_step = tf.compat.v1.train.get_or_create_global_step()
        collect_policy = tf_agent.collect_policy()

        collect_op = dynamic_episode_driver.DynamicEpisodeDriver(
            tf_env,
            collect_policy,
            observers=replay_buffer_observer + train_metrics,
            num_episodes=collect_episodes_per_iteration).run()

        # trajectories, _ = iterator.get_next()
        trajectories = replay_buffer.gather_all()

        train_op, _ = tf_agent.train(
            experience=trajectories, train_step_counter=global_step)

        with tf.control_dependencies([train_op]):
            clear_replay_op = replay_buffer.clear()

        with tf.control_dependencies([clear_replay_op]):
            train_op = tf.identity(train_op)

        train_checkpointer = common_utils.Checkpointer(
            ckpt_dir=train_dir,
            agent=tf_agent,
            global_step=global_step,
            metrics=tf.contrib.checkpoint.List(train_metrics))
        policy_checkpointer = common_utils.Checkpointer(
            ckpt_dir=os.path.join(train_dir, 'policy'),
            policy=tf_agent.policy(),
            global_step=global_step)
        rb_checkpointer = common_utils.Checkpointer(
            ckpt_dir=os.path.join(train_dir, 'replay_buffer'),
            max_to_keep=1,
            replay_buffer=replay_buffer)

        for train_metric in train_metrics:
            train_metric.tf_summaries()
        summary_op = tf.contrib.summary.all_summary_ops()

        with eval_summary_writer.as_default(), \
             tf.contrib.summary.always_record_summaries():
            for eval_metric in eval_metrics:
                eval_metric.tf_summaries(step_metrics=step_metrics)

        init_agent_op = tf_agent.initialize()

        config = tf.ConfigProto()
        config.gpu_options.allow_growth = True
        with tf.compat.v1.Session(tf_master, config=config) as sess:
            # Initialize graph.
            train_checkpointer.initialize_or_restore(sess)
            rb_checkpointer.initialize_or_restore(sess)
            # sess.run(iterator.initializer)

            # TODO(sguada) Remove once Periodically can be saved.
            common_utils.initialize_uninitialized_variables(sess)
            sess.run(init_agent_op)

            # tf.contrib.summary.initialize(session=sess, graph=tf.get_default_graph())
            tf.contrib.summary.initialize(session=sess)

            collect_time = 0
            train_time = 0
            timed_at_step = sess.run(global_step)
            steps_per_second_ph = tf.compat.v1.placeholder(
                tf.float32, shape=(), name='steps_per_sec_ph')
            steps_per_second_summary = tf.contrib.summary.scalar(
                name='global_steps/sec', tensor=steps_per_second_ph)

            while sess.run(environment_steps_count) < num_environment_steps:
                global_step_val = sess.run(global_step)
                if global_step_val % eval_interval == 0:
                    metric_utils.compute_summaries(
                        eval_metrics,
                        eval_py_env,
                        eval_py_policy,
                        num_episodes=num_eval_episodes,
                        global_step=global_step_val,
                        callback=eval_metrics_callback,
                    )
                    sess.run(eval_summary_writer_flush_op)

                start_time = time.time()
                sess.run(collect_op)
                collect_time += time.time() - start_time
                print('collect:', time.time() - start_time)

                valid_range = sess.run(valid_range_op)
                print('valid_range', valid_range)

                start_time = time.time()
                total_loss, _ = sess.run([train_op, summary_op])
                train_time += time.time() - start_time
                print('train:', time.time() - start_time)

                if global_step_val % log_interval == 0:
                    logging.info('step = %d, loss = %f', global_step_val, total_loss)
                    steps_per_sec = (
                            (global_step_val - timed_at_step) / (collect_time + train_time))
                    logging.info('%.3f steps/sec', steps_per_sec)
                    sess.run(
                        steps_per_second_summary,
                        feed_dict={steps_per_second_ph: steps_per_sec})
                    logging.info('collect_time = {}, train_time = {}'.format(
                        collect_time, train_time))
                    timed_at_step = global_step_val
                    collect_time = 0
                    train_time = 0

                if global_step_val % train_checkpoint_interval == 0:
                    train_checkpointer.save(global_step=global_step_val)

                if global_step_val % policy_checkpoint_interval == 0:
                    policy_checkpointer.save(global_step=global_step_val)

                if global_step_val % rb_checkpoint_interval == 0:
                    rb_checkpointer.save(global_step=global_step_val)

            # One final eval before exiting.
            metric_utils.compute_summaries(
                eval_metrics,
                eval_py_env,
                eval_py_policy,
                num_episodes=num_eval_episodes,
                global_step=global_step_val,
                callback=eval_metrics_callback,
            )
            sess.run(eval_summary_writer_flush_op)


def main(_):
    os.environ["CUDA_VISIBLE_DEVICES"] = FLAGS.gpu_c

    if tf.executing_eagerly():
        return
    tf.logging.set_verbosity(tf.logging.INFO)
    train_eval(
        FLAGS.root_dir,
        gpu=FLAGS.gpu_g,
        tf_master=FLAGS.master,
        env_load_fn=lambda mode, device_idx: env_load_fn(FLAGS.config_file, mode, FLAGS.physics_timestep, device_idx),
        env_mode=FLAGS.mode,
        batch_size=FLAGS.batch_size,
        conv_layer_params=((32, (8, 8), 4), (64, (4, 4), 2), (64, (3, 3), 1)),
        replay_buffer_capacity=FLAGS.replay_buffer_capacity,
        num_environment_steps=FLAGS.num_environment_steps,
        num_parallel_environments=FLAGS.num_parallel_environments,
        num_epochs=FLAGS.num_epochs,
        collect_episodes_per_iteration=FLAGS.collect_episodes_per_iteration,
        num_eval_episodes=FLAGS.num_eval_episodes,
        use_rnns=FLAGS.use_rnns)


if __name__ == '__main__':
    flags.mark_flag_as_required('root_dir')
    tf.app.run()
