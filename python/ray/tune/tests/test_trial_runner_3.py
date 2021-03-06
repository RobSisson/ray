import time
from collections import Counter
import os
import pickle
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

import ray
from ray.cluster_utils import Cluster
from ray.rllib import _register_all

from ray import tune
from ray.tune import Callback, TuneError
from ray.tune.ray_trial_executor import RayTrialExecutor
from ray.tune.result import TRAINING_ITERATION
from ray.tune.schedulers import TrialScheduler, FIFOScheduler
from ray.tune.experiment import Experiment
from ray.tune.trial import Trial
from ray.tune.trial_runner import TrialRunner
from ray.tune.resources import Resources, json_to_resources, resources_to_json
from ray.tune.suggest.repeater import Repeater
from ray.tune.suggest._mock import _MockSuggestionAlgorithm
from ray.tune.suggest.suggestion import Searcher, ConcurrencyLimiter
from ray.tune.suggest.search_generator import SearchGenerator
from ray.util import placement_group


class TrialRunnerTest3(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        ray.shutdown()
        _register_all()  # re-register the evicted objects
        if "CUDA_VISIBLE_DEVICES" in os.environ:
            del os.environ["CUDA_VISIBLE_DEVICES"]
        shutil.rmtree(self.tmpdir)

    def testStepHook(self):
        ray.init(num_cpus=4, num_gpus=2)
        runner = TrialRunner()

        def on_step_begin(self, trialrunner):
            self._update_avail_resources()
            cnt = self.pre_step if hasattr(self, "pre_step") else 0
            self.pre_step = cnt + 1

        def on_step_end(self, trialrunner):
            cnt = self.pre_step if hasattr(self, "post_step") else 0
            self.post_step = 1 + cnt

        import types
        runner.trial_executor.on_step_begin = types.MethodType(
            on_step_begin, runner.trial_executor)
        runner.trial_executor.on_step_end = types.MethodType(
            on_step_end, runner.trial_executor)

        kwargs = {
            "stopping_criterion": {
                "training_iteration": 5
            },
            "resources": Resources(cpu=1, gpu=1),
        }
        runner.add_trial(Trial("__fake", **kwargs))
        runner.step()
        self.assertEqual(runner.trial_executor.pre_step, 1)
        self.assertEqual(runner.trial_executor.post_step, 1)

    def testStopTrial(self):
        ray.init(num_cpus=4, num_gpus=2)
        runner = TrialRunner()
        kwargs = {
            "stopping_criterion": {
                "training_iteration": 5
            },
            "resources": Resources(cpu=1, gpu=1),
        }
        trials = [
            Trial("__fake", **kwargs),
            Trial("__fake", **kwargs),
            Trial("__fake", **kwargs),
            Trial("__fake", **kwargs)
        ]
        for t in trials:
            runner.add_trial(t)
        runner.step()
        self.assertEqual(trials[0].status, Trial.RUNNING)
        self.assertEqual(trials[1].status, Trial.PENDING)

        # Stop trial while running
        runner.stop_trial(trials[0])
        self.assertEqual(trials[0].status, Trial.TERMINATED)
        self.assertEqual(trials[1].status, Trial.PENDING)

        runner.step()
        self.assertEqual(trials[0].status, Trial.TERMINATED)
        self.assertEqual(trials[1].status, Trial.RUNNING)
        self.assertEqual(trials[-1].status, Trial.PENDING)

        # Stop trial while pending
        runner.stop_trial(trials[-1])
        self.assertEqual(trials[0].status, Trial.TERMINATED)
        self.assertEqual(trials[1].status, Trial.RUNNING)
        self.assertEqual(trials[-1].status, Trial.TERMINATED)

        runner.step()
        self.assertEqual(trials[0].status, Trial.TERMINATED)
        self.assertEqual(trials[1].status, Trial.RUNNING)
        self.assertEqual(trials[2].status, Trial.RUNNING)
        self.assertEqual(trials[-1].status, Trial.TERMINATED)

    def testSearchAlgNotification(self):
        """Checks notification of trial to the Search Algorithm."""
        ray.init(num_cpus=4, num_gpus=2)
        experiment_spec = {"run": "__fake", "stop": {"training_iteration": 2}}
        experiments = [Experiment.from_json("test", experiment_spec)]
        search_alg = _MockSuggestionAlgorithm()
        searcher = search_alg.searcher
        search_alg.add_configurations(experiments)
        runner = TrialRunner(search_alg=search_alg)
        runner.step()
        trials = runner.get_trials()
        self.assertEqual(trials[0].status, Trial.RUNNING)

        runner.step()
        self.assertEqual(trials[0].status, Trial.RUNNING)

        runner.step()
        self.assertEqual(trials[0].status, Trial.TERMINATED)

        self.assertEqual(searcher.counter["result"], 1)
        self.assertEqual(searcher.counter["complete"], 1)

    def testSearchAlgFinished(self):
        """Checks that SearchAlg is Finished before all trials are done."""
        ray.init(num_cpus=4, local_mode=True, include_dashboard=False)
        experiment_spec = {"run": "__fake", "stop": {"training_iteration": 1}}
        experiments = [Experiment.from_json("test", experiment_spec)]
        searcher = _MockSuggestionAlgorithm()
        searcher.add_configurations(experiments)
        runner = TrialRunner(search_alg=searcher)
        runner.step()
        trials = runner.get_trials()
        self.assertEqual(trials[0].status, Trial.RUNNING)
        self.assertTrue(searcher.is_finished())
        self.assertFalse(runner.is_finished())

        runner.step()
        self.assertEqual(trials[0].status, Trial.TERMINATED)
        self.assertEqual(len(searcher.live_trials), 0)
        self.assertTrue(searcher.is_finished())
        self.assertTrue(runner.is_finished())

    def testSearchAlgSchedulerInteraction(self):
        """Checks that TrialScheduler killing trial will notify SearchAlg."""

        class _MockScheduler(FIFOScheduler):
            def on_trial_result(self, *args, **kwargs):
                return TrialScheduler.STOP

        ray.init(num_cpus=4, local_mode=True, include_dashboard=False)
        experiment_spec = {"run": "__fake", "stop": {"training_iteration": 2}}
        experiments = [Experiment.from_json("test", experiment_spec)]
        searcher = _MockSuggestionAlgorithm()
        searcher.add_configurations(experiments)
        runner = TrialRunner(search_alg=searcher, scheduler=_MockScheduler())
        runner.step()
        trials = runner.get_trials()
        self.assertEqual(trials[0].status, Trial.RUNNING)
        self.assertTrue(searcher.is_finished())
        self.assertFalse(runner.is_finished())

        runner.step()
        self.assertEqual(trials[0].status, Trial.TERMINATED)
        self.assertEqual(len(searcher.live_trials), 0)
        self.assertTrue(searcher.is_finished())
        self.assertTrue(runner.is_finished())

    def testSearchAlgStalled(self):
        """Checks that runner and searcher state is maintained when stalled."""
        ray.init(num_cpus=4, num_gpus=2)
        experiment_spec = {
            "run": "__fake",
            "num_samples": 3,
            "stop": {
                "training_iteration": 1
            }
        }
        experiments = [Experiment.from_json("test", experiment_spec)]
        search_alg = _MockSuggestionAlgorithm(max_concurrent=1)
        search_alg.add_configurations(experiments)
        searcher = search_alg.searcher
        runner = TrialRunner(search_alg=search_alg)
        runner.step()
        trials = runner.get_trials()
        self.assertEqual(trials[0].status, Trial.RUNNING)

        runner.step()
        self.assertEqual(trials[0].status, Trial.TERMINATED)

        trials = runner.get_trials()
        runner.step()
        self.assertEqual(trials[1].status, Trial.RUNNING)
        self.assertEqual(len(searcher.live_trials), 1)

        searcher.stall = True

        runner.step()
        self.assertEqual(trials[1].status, Trial.TERMINATED)
        self.assertEqual(len(searcher.live_trials), 0)

        self.assertTrue(all(trial.is_finished() for trial in trials))
        self.assertFalse(search_alg.is_finished())
        self.assertFalse(runner.is_finished())

        searcher.stall = False

        runner.step()
        trials = runner.get_trials()
        self.assertEqual(trials[2].status, Trial.RUNNING)
        self.assertEqual(len(searcher.live_trials), 1)

        runner.step()
        self.assertEqual(trials[2].status, Trial.TERMINATED)
        self.assertEqual(len(searcher.live_trials), 0)
        self.assertTrue(search_alg.is_finished())
        self.assertTrue(runner.is_finished())

    def testSearchAlgFinishes(self):
        """Empty SearchAlg changing state in `next_trials` does not crash."""

        class FinishFastAlg(_MockSuggestionAlgorithm):
            _index = 0

            def next_trial(self):
                spec = self._experiment.spec
                trial = None
                if self._index < spec["num_samples"]:
                    trial = Trial(
                        spec.get("run"), stopping_criterion=spec.get("stop"))
                self._index += 1

                if self._index > 4:
                    self.set_finished()

                return trial

            def suggest(self, trial_id):
                return {}

        ray.init(num_cpus=2, local_mode=True, include_dashboard=False)
        experiment_spec = {
            "run": "__fake",
            "num_samples": 2,
            "stop": {
                "training_iteration": 1
            }
        }
        searcher = FinishFastAlg()
        experiments = [Experiment.from_json("test", experiment_spec)]
        searcher.add_configurations(experiments)

        runner = TrialRunner(search_alg=searcher)
        self.assertFalse(runner.is_finished())
        runner.step()  # This launches a new run
        runner.step()  # This launches a 2nd run
        self.assertFalse(searcher.is_finished())
        self.assertFalse(runner.is_finished())
        runner.step()  # This kills the first run
        self.assertFalse(searcher.is_finished())
        self.assertFalse(runner.is_finished())
        runner.step()  # This kills the 2nd run
        self.assertFalse(searcher.is_finished())
        self.assertFalse(runner.is_finished())
        runner.step()  # this converts self._finished to True
        self.assertTrue(searcher.is_finished())
        self.assertRaises(TuneError, runner.step)

    def testSearcherSaveRestore(self):
        ray.init(num_cpus=8, local_mode=True)

        def create_searcher():
            class TestSuggestion(Searcher):
                def __init__(self, index):
                    self.index = index
                    self.returned_result = []
                    super().__init__(metric="result", mode="max")

                def suggest(self, trial_id):
                    self.index += 1
                    return {"test_variable": self.index}

                def on_trial_complete(self, trial_id, result=None, **kwargs):
                    self.returned_result.append(result)

                def save(self, checkpoint_path):
                    with open(checkpoint_path, "wb") as f:
                        pickle.dump(self.__dict__, f)

                def restore(self, checkpoint_path):
                    with open(checkpoint_path, "rb") as f:
                        self.__dict__.update(pickle.load(f))

            searcher = TestSuggestion(0)
            searcher = ConcurrencyLimiter(searcher, max_concurrent=2)
            searcher = Repeater(searcher, repeat=3, set_index=False)
            search_alg = SearchGenerator(searcher)
            experiment_spec = {
                "run": "__fake",
                "num_samples": 20,
                "stop": {
                    "training_iteration": 2
                }
            }
            experiments = [Experiment.from_json("test", experiment_spec)]
            search_alg.add_configurations(experiments)
            return search_alg

        searcher = create_searcher()
        runner = TrialRunner(
            search_alg=searcher,
            local_checkpoint_dir=self.tmpdir,
            checkpoint_period=-1)
        for i in range(6):
            runner.step()

        assert len(
            runner.get_trials()) == 6, [t.config for t in runner.get_trials()]
        runner.checkpoint()
        trials = runner.get_trials()
        [
            runner.trial_executor.stop_trial(t) for t in trials
            if t.status is not Trial.ERROR
        ]
        del runner
        # stop_all(runner.get_trials())

        searcher = create_searcher()
        runner2 = TrialRunner(
            search_alg=searcher,
            local_checkpoint_dir=self.tmpdir,
            resume="LOCAL")
        assert len(runner2.get_trials()) == 6, [
            t.config for t in runner2.get_trials()
        ]

        def trial_statuses():
            return [t.status for t in runner2.get_trials()]

        def num_running_trials():
            return sum(t.status == Trial.RUNNING for t in runner2.get_trials())

        for i in range(6):
            runner2.step()
        assert len(set(trial_statuses())) == 1
        assert Trial.RUNNING in trial_statuses()
        for i in range(20):
            runner2.step()
            assert 1 <= num_running_trials() <= 6
        evaluated = [
            t.evaluated_params["test_variable"] for t in runner2.get_trials()
        ]
        count = Counter(evaluated)
        assert all(v <= 3 for v in count.values())

    def testTrialErrorResumeFalse(self):
        ray.init(num_cpus=3, local_mode=True, include_dashboard=False)
        runner = TrialRunner(local_checkpoint_dir=self.tmpdir)
        kwargs = {
            "stopping_criterion": {
                "training_iteration": 4
            },
            "resources": Resources(cpu=1, gpu=0),
        }
        trials = [
            Trial("__fake", config={"mock_error": True}, **kwargs),
            Trial("__fake", **kwargs),
            Trial("__fake", **kwargs),
        ]
        for t in trials:
            runner.add_trial(t)

        while not runner.is_finished():
            runner.step()

        runner.checkpoint(force=True)

        assert trials[0].status == Trial.ERROR
        del runner

        new_runner = TrialRunner(resume=True, local_checkpoint_dir=self.tmpdir)
        assert len(new_runner.get_trials()) == 3
        assert Trial.ERROR in (t.status for t in new_runner.get_trials())

    def testTrialErrorResumeTrue(self):
        ray.init(num_cpus=3, local_mode=True, include_dashboard=False)
        runner = TrialRunner(local_checkpoint_dir=self.tmpdir)
        kwargs = {
            "stopping_criterion": {
                "training_iteration": 4
            },
            "resources": Resources(cpu=1, gpu=0),
        }
        trials = [
            Trial("__fake", config={"mock_error": True}, **kwargs),
            Trial("__fake", **kwargs),
            Trial("__fake", **kwargs),
        ]
        for t in trials:
            runner.add_trial(t)

        while not runner.is_finished():
            runner.step()

        runner.checkpoint(force=True)

        assert trials[0].status == Trial.ERROR
        del runner

        new_runner = TrialRunner(
            resume="ERRORED_ONLY", local_checkpoint_dir=self.tmpdir)
        assert len(new_runner.get_trials()) == 3
        assert Trial.ERROR not in (t.status for t in new_runner.get_trials())
        # The below is just a check for standard behavior.
        disable_error = False
        for t in new_runner.get_trials():
            if t.config.get("mock_error"):
                t.config["mock_error"] = False
                disable_error = True
        assert disable_error

        while not new_runner.is_finished():
            new_runner.step()
        assert Trial.ERROR not in (t.status for t in new_runner.get_trials())

    def testTrialSaveRestore(self):
        """Creates different trials to test runner.checkpoint/restore."""
        ray.init(num_cpus=3)

        runner = TrialRunner(
            local_checkpoint_dir=self.tmpdir, checkpoint_period=0)
        trials = [
            Trial(
                "__fake",
                trial_id="trial_terminate",
                stopping_criterion={"training_iteration": 1},
                checkpoint_freq=1)
        ]
        runner.add_trial(trials[0])
        runner.step()  # Start trial
        runner.step()  # Process result, dispatch save
        runner.step()  # Process save
        self.assertEqual(trials[0].status, Trial.TERMINATED)

        trials += [
            Trial(
                "__fake",
                trial_id="trial_fail",
                stopping_criterion={"training_iteration": 3},
                checkpoint_freq=1,
                config={"mock_error": True})
        ]
        runner.add_trial(trials[1])
        runner.step()  # Start trial
        runner.step()  # Process result, dispatch save
        runner.step()  # Process save
        runner.step()  # Error
        self.assertEqual(trials[1].status, Trial.ERROR)

        trials += [
            Trial(
                "__fake",
                trial_id="trial_succ",
                stopping_criterion={"training_iteration": 2},
                checkpoint_freq=1)
        ]
        runner.add_trial(trials[2])
        runner.step()  # Start trial
        self.assertEqual(len(runner.trial_executor.get_checkpoints()), 3)
        self.assertEqual(trials[2].status, Trial.RUNNING)

        runner2 = TrialRunner(resume="LOCAL", local_checkpoint_dir=self.tmpdir)
        for tid in ["trial_terminate", "trial_fail"]:
            original_trial = runner.get_trial(tid)
            restored_trial = runner2.get_trial(tid)
            self.assertEqual(original_trial.status, restored_trial.status)

        restored_trial = runner2.get_trial("trial_succ")
        self.assertEqual(Trial.PENDING, restored_trial.status)

        runner2.step()  # Start trial
        runner2.step()  # Process result, dispatch save
        runner2.step()  # Process save
        runner2.step()  # Process result, dispatch save
        runner2.step()  # Process save
        self.assertRaises(TuneError, runner2.step)

    def testTrialNoSave(self):
        """Check that non-checkpointing trials are not saved."""
        ray.init(num_cpus=3)

        runner = TrialRunner(
            local_checkpoint_dir=self.tmpdir, checkpoint_period=0)
        runner.add_trial(
            Trial(
                "__fake",
                trial_id="non_checkpoint",
                stopping_criterion={"training_iteration": 2}))

        while not all(t.status == Trial.TERMINATED
                      for t in runner.get_trials()):
            runner.step()

        runner.add_trial(
            Trial(
                "__fake",
                trial_id="checkpoint",
                checkpoint_at_end=True,
                stopping_criterion={"training_iteration": 2}))

        while not all(t.status == Trial.TERMINATED
                      for t in runner.get_trials()):
            runner.step()

        runner.add_trial(
            Trial(
                "__fake",
                trial_id="pending",
                stopping_criterion={"training_iteration": 2}))

        runner.step()
        runner.step()

        runner2 = TrialRunner(resume="LOCAL", local_checkpoint_dir=self.tmpdir)
        new_trials = runner2.get_trials()
        self.assertEqual(len(new_trials), 3)
        self.assertTrue(
            runner2.get_trial("non_checkpoint").status == Trial.TERMINATED)
        self.assertTrue(
            runner2.get_trial("checkpoint").status == Trial.TERMINATED)
        self.assertTrue(runner2.get_trial("pending").status == Trial.PENDING)
        self.assertTrue(not runner2.get_trial("pending").last_result)
        runner2.step()

    def testCheckpointWithFunction(self):
        ray.init(num_cpus=2)

        trial = Trial(
            "__fake",
            config={"callbacks": {
                "on_episode_start": lambda i: i,
            }},
            checkpoint_freq=1)
        runner = TrialRunner(
            local_checkpoint_dir=self.tmpdir, checkpoint_period=0)
        runner.add_trial(trial)
        for _ in range(5):
            runner.step()
        # force checkpoint
        runner.checkpoint()
        runner2 = TrialRunner(resume="LOCAL", local_checkpoint_dir=self.tmpdir)
        new_trial = runner2.get_trials()[0]
        self.assertTrue("callbacks" in new_trial.config)
        self.assertTrue("on_episode_start" in new_trial.config["callbacks"])

    def testCheckpointOverwrite(self):
        def count_checkpoints(cdir):
            return sum((fname.startswith("experiment_state")
                        and fname.endswith(".json"))
                       for fname in os.listdir(cdir))

        ray.init(num_cpus=2)

        trial = Trial("__fake", checkpoint_freq=1)
        tmpdir = tempfile.mkdtemp()
        runner = TrialRunner(local_checkpoint_dir=tmpdir, checkpoint_period=0)
        runner.add_trial(trial)
        for _ in range(5):
            runner.step()
        # force checkpoint
        runner.checkpoint()
        self.assertEqual(count_checkpoints(tmpdir), 1)

        runner2 = TrialRunner(resume="LOCAL", local_checkpoint_dir=tmpdir)
        for _ in range(5):
            runner2.step()
        self.assertEqual(count_checkpoints(tmpdir), 2)

        runner2.checkpoint()
        self.assertEqual(count_checkpoints(tmpdir), 2)
        shutil.rmtree(tmpdir)

    @patch("ray.tune.ray_trial_executor.TUNE_RESULT_BUFFER_MIN_TIME_S", 0.5)
    @patch("ray.tune.ray_trial_executor.TUNE_RESULT_BUFFER_LENGTH", 7)
    def testCheckpointFreqBuffered(self):
        def num_checkpoints(trial):
            return sum(
                item.startswith("checkpoint_")
                for item in os.listdir(trial.logdir))

        ray.init(num_cpus=2)

        trial = Trial("__fake", checkpoint_freq=3)
        runner = TrialRunner(
            local_checkpoint_dir=self.tmpdir, checkpoint_period=0)
        runner.add_trial(trial)

        runner.step()  # start trial
        runner.step()  # run iteration 1-3
        runner.step()  # process save
        self.assertEqual(trial.last_result[TRAINING_ITERATION], 3)
        self.assertEqual(num_checkpoints(trial), 1)

        runner.step()  # run iteration 4-6
        runner.step()  # process save
        self.assertEqual(trial.last_result[TRAINING_ITERATION], 6)
        self.assertEqual(num_checkpoints(trial), 2)

        runner.step()  # run iteration 7-9
        runner.step()  # process save
        self.assertEqual(trial.last_result[TRAINING_ITERATION], 9)
        self.assertEqual(num_checkpoints(trial), 3)

    def testUserCheckpoint(self):
        ray.init(num_cpus=3)
        runner = TrialRunner(
            local_checkpoint_dir=self.tmpdir, checkpoint_period=0)
        runner.add_trial(Trial("__fake", config={"user_checkpoint_freq": 2}))
        trials = runner.get_trials()

        runner.step()  # Start trial
        self.assertEqual(trials[0].status, Trial.RUNNING)
        self.assertEqual(ray.get(trials[0].runner.set_info.remote(1)), 1)
        runner.step()  # Process result
        self.assertFalse(trials[0].has_checkpoint())
        runner.step()  # Process result
        self.assertFalse(trials[0].has_checkpoint())
        runner.step()  # Process result, dispatch save
        runner.step()  # Process save
        self.assertTrue(trials[0].has_checkpoint())

        runner2 = TrialRunner(resume="LOCAL", local_checkpoint_dir=self.tmpdir)
        runner2.step()  # 5: Start trial and dispatch restore
        trials2 = runner2.get_trials()
        self.assertEqual(ray.get(trials2[0].runner.get_info.remote()), 1)

    @patch("ray.tune.ray_trial_executor.TUNE_RESULT_BUFFER_MIN_TIME_S", 1)
    @patch("ray.tune.ray_trial_executor.TUNE_RESULT_BUFFER_LENGTH", 8)
    def testUserCheckpointBuffered(self):
        def num_checkpoints(trial):
            return sum(
                item.startswith("checkpoint_")
                for item in os.listdir(trial.logdir))

        ray.init(num_cpus=3)
        runner = TrialRunner(
            local_checkpoint_dir=self.tmpdir, checkpoint_period=0)
        runner.add_trial(Trial("__fake", config={"user_checkpoint_freq": 10}))
        trials = runner.get_trials()

        runner.step()  # Start trial, schedule 1-8
        self.assertEqual(trials[0].status, Trial.RUNNING)
        self.assertEqual(ray.get(trials[0].runner.set_info.remote(1)), 1)
        self.assertEqual(num_checkpoints(trials[0]), 0)

        runner.step()  # Process results 0-8, schedule 9-11 (CP)
        self.assertEqual(trials[0].last_result.get(TRAINING_ITERATION), 8)
        self.assertFalse(trials[0].has_checkpoint())
        self.assertEqual(num_checkpoints(trials[0]), 0)

        runner.step()  # Process results 9-11
        runner.step()  # handle CP, schedule 12-19
        self.assertEqual(trials[0].last_result.get(TRAINING_ITERATION), 11)
        self.assertTrue(trials[0].has_checkpoint())
        self.assertEqual(num_checkpoints(trials[0]), 1)

        runner.step()  # Process results 12-19, schedule 20-21
        self.assertEqual(trials[0].last_result.get(TRAINING_ITERATION), 19)
        self.assertTrue(trials[0].has_checkpoint())
        self.assertEqual(num_checkpoints(trials[0]), 1)

        runner.step()  # Process results 20-21
        runner.step()  # handle CP, schedule 21-29
        self.assertEqual(trials[0].last_result.get(TRAINING_ITERATION), 21)
        self.assertTrue(trials[0].has_checkpoint())
        self.assertEqual(num_checkpoints(trials[0]), 2)

        runner.step()  # Process results 21-29, schedule 30-31
        self.assertEqual(trials[0].last_result.get(TRAINING_ITERATION), 29)
        self.assertTrue(trials[0].has_checkpoint())
        self.assertTrue(trials[0].has_checkpoint())
        self.assertEqual(num_checkpoints(trials[0]), 2)


class SearchAlgorithmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ray.init(
            num_cpus=4, num_gpus=0, local_mode=True, include_dashboard=False)

    @classmethod
    def tearDownClass(cls):
        ray.shutdown()
        _register_all()

    def testNestedSuggestion(self):
        class TestSuggestion(Searcher):
            def suggest(self, trial_id):
                return {"a": {"b": {"c": {"d": 4, "e": 5}}}}

        searcher = TestSuggestion()
        alg = SearchGenerator(searcher)
        alg.add_configurations({"test": {"run": "__fake"}})
        trial = alg.next_trial()
        self.assertTrue("e=5" in trial.experiment_tag)
        self.assertTrue("d=4" in trial.experiment_tag)

    def _test_repeater(self, num_samples, repeat):
        class TestSuggestion(Searcher):
            index = 0

            def suggest(self, trial_id):
                self.index += 1
                return {"test_variable": 5 + self.index}

            def on_trial_complete(self, *args, **kwargs):
                return

        searcher = TestSuggestion(metric="episode_reward_mean")
        repeat_searcher = Repeater(searcher, repeat=repeat, set_index=False)
        alg = SearchGenerator(repeat_searcher)
        experiment_spec = {
            "run": "__fake",
            "num_samples": num_samples,
            "stop": {
                "training_iteration": 1
            }
        }
        alg.add_configurations({"test": experiment_spec})
        runner = TrialRunner(search_alg=alg)
        while not runner.is_finished():
            runner.step()

        return runner.get_trials()

    def testRepeat1(self):
        trials = self._test_repeater(num_samples=2, repeat=1)
        self.assertEqual(len(trials), 2)
        parameter_set = {t.evaluated_params["test_variable"] for t in trials}
        self.assertEqual(len(parameter_set), 2)

    def testRepeat4(self):
        trials = self._test_repeater(num_samples=12, repeat=4)
        self.assertEqual(len(trials), 12)
        parameter_set = {t.evaluated_params["test_variable"] for t in trials}
        self.assertEqual(len(parameter_set), 3)

    def testOddRepeat(self):
        trials = self._test_repeater(num_samples=11, repeat=5)
        self.assertEqual(len(trials), 11)
        parameter_set = {t.evaluated_params["test_variable"] for t in trials}
        self.assertEqual(len(parameter_set), 3)

    def testSetGetRepeater(self):
        class TestSuggestion(Searcher):
            def __init__(self, index):
                self.index = index
                self.returned_result = []
                super().__init__(metric="result", mode="max")

            def suggest(self, trial_id):
                self.index += 1
                return {"score": self.index}

            def on_trial_complete(self, trial_id, result=None, **kwargs):
                self.returned_result.append(result)

        searcher = TestSuggestion(0)
        repeater1 = Repeater(searcher, repeat=3, set_index=False)
        for i in range(3):
            assert repeater1.suggest(f"test_{i}")["score"] == 1
        for i in range(2):  # An incomplete set of results
            assert repeater1.suggest(f"test_{i}_2")["score"] == 2

        # Restore a new one
        state = repeater1.get_state()
        del repeater1
        new_repeater = Repeater(searcher, repeat=1, set_index=True)
        new_repeater.set_state(state)
        assert new_repeater.repeat == 3
        assert new_repeater.suggest("test_2_2")["score"] == 2
        assert new_repeater.suggest("test_x")["score"] == 3

        # Report results
        for i in range(3):
            new_repeater.on_trial_complete(f"test_{i}", {"result": 2})

        for i in range(3):
            new_repeater.on_trial_complete(f"test_{i}_2", {"result": -i * 10})

        assert len(new_repeater.searcher.returned_result) == 2
        assert new_repeater.searcher.returned_result[-1] == {"result": -10}

        # Finish the rest of the last trial group
        new_repeater.on_trial_complete("test_x", {"result": 3})
        assert new_repeater.suggest("test_y")["score"] == 3
        new_repeater.on_trial_complete("test_y", {"result": 3})
        assert len(new_repeater.searcher.returned_result) == 2
        assert new_repeater.suggest("test_z")["score"] == 3
        new_repeater.on_trial_complete("test_z", {"result": 3})
        assert len(new_repeater.searcher.returned_result) == 3
        assert new_repeater.searcher.returned_result[-1] == {"result": 3}

    def testSetGetLimiter(self):
        class TestSuggestion(Searcher):
            def __init__(self, index):
                self.index = index
                self.returned_result = []
                super().__init__(metric="result", mode="max")

            def suggest(self, trial_id):
                self.index += 1
                return {"score": self.index}

            def on_trial_complete(self, trial_id, result=None, **kwargs):
                self.returned_result.append(result)

        searcher = TestSuggestion(0)
        limiter = ConcurrencyLimiter(searcher, max_concurrent=2)
        assert limiter.suggest("test_1")["score"] == 1
        assert limiter.suggest("test_2")["score"] == 2
        assert limiter.suggest("test_3") is None

        state = limiter.get_state()
        del limiter
        limiter2 = ConcurrencyLimiter(searcher, max_concurrent=3)
        limiter2.set_state(state)
        assert limiter2.suggest("test_4") is None
        assert limiter2.suggest("test_5") is None
        limiter2.on_trial_complete("test_1", {"result": 3})
        limiter2.on_trial_complete("test_2", {"result": 3})
        assert limiter2.suggest("test_3")["score"] == 3

    def testBatchLimiter(self):
        class TestSuggestion(Searcher):
            def __init__(self, index):
                self.index = index
                self.returned_result = []
                super().__init__(metric="result", mode="max")

            def suggest(self, trial_id):
                self.index += 1
                return {"score": self.index}

            def on_trial_complete(self, trial_id, result=None, **kwargs):
                self.returned_result.append(result)

        searcher = TestSuggestion(0)
        limiter = ConcurrencyLimiter(searcher, max_concurrent=2, batch=True)
        assert limiter.suggest("test_1")["score"] == 1
        assert limiter.suggest("test_2")["score"] == 2
        assert limiter.suggest("test_3") is None

        limiter.on_trial_complete("test_1", {"result": 3})
        assert limiter.suggest("test_3") is None
        limiter.on_trial_complete("test_2", {"result": 3})
        assert limiter.suggest("test_3") is not None


class ResourcesTest(unittest.TestCase):
    def testSubtraction(self):
        resource_1 = Resources(
            1,
            0,
            0,
            1,
            custom_resources={
                "a": 1,
                "b": 2
            },
            extra_custom_resources={
                "a": 1,
                "b": 1
            })
        resource_2 = Resources(
            1,
            0,
            0,
            1,
            custom_resources={
                "a": 1,
                "b": 2
            },
            extra_custom_resources={
                "a": 1,
                "b": 1
            })
        new_res = Resources.subtract(resource_1, resource_2)
        self.assertTrue(new_res.cpu == 0)
        self.assertTrue(new_res.gpu == 0)
        self.assertTrue(new_res.extra_cpu == 0)
        self.assertTrue(new_res.extra_gpu == 0)
        self.assertTrue(all(k == 0 for k in new_res.custom_resources.values()))
        self.assertTrue(
            all(k == 0 for k in new_res.extra_custom_resources.values()))

    def testDifferentResources(self):
        resource_1 = Resources(1, 0, 0, 1, custom_resources={"a": 1, "b": 2})
        resource_2 = Resources(1, 0, 0, 1, custom_resources={"a": 1, "c": 2})
        new_res = Resources.subtract(resource_1, resource_2)
        assert "c" in new_res.custom_resources
        assert "b" in new_res.custom_resources
        self.assertTrue(new_res.cpu == 0)
        self.assertTrue(new_res.gpu == 0)
        self.assertTrue(new_res.extra_cpu == 0)
        self.assertTrue(new_res.extra_gpu == 0)
        self.assertTrue(new_res.get("a") == 0)

    def testSerialization(self):
        original = Resources(1, 0, 0, 1, custom_resources={"a": 1, "b": 2})
        jsoned = resources_to_json(original)
        new_resource = json_to_resources(jsoned)
        self.assertEqual(original, new_resource)


class TrialRunnerPlacementGroupTest(unittest.TestCase):
    def setUp(self):
        os.environ["TUNE_GLOBAL_CHECKPOINT_S"] = "10000"
        self.head_cpus = 8
        self.head_gpus = 4
        self.head_custom = 16

        self.cluster = Cluster(
            initialize_head=True,
            connect=True,
            head_node_args={
                "num_cpus": self.head_cpus,
                "num_gpus": self.head_gpus,
                "resources": {
                    "custom": self.head_custom
                },
                "_system_config": {
                    "num_heartbeats_timeout": 10
                }
            })
        # Pytest doesn't play nicely with imports
        _register_all()

    def tearDown(self):
        ray.shutdown()
        self.cluster.shutdown()
        _register_all()  # re-register the evicted objects

    def testPlacementGroupRequests(self, scheduled=10):
        """In this test we try to start 10 trials but only have resources
        for 2. Placement groups should still be created and PENDING.

        Eventually they should be scheduled sequentially (i.e. in pairs
        of two)."""

        def train(config):
            time.sleep(1)
            now = time.time()
            tune.report(end=now - config["start_time"])

        def placement_group_factory():
            head_bundle = {"CPU": 4, "GPU": 0, "custom": 0}
            child_bundle = {"custom": 1}

            return placement_group([head_bundle, child_bundle, child_bundle])

        trial_executor = RayTrialExecutor()

        this = self

        class _TestCallback(Callback):
            def on_step_end(self, iteration, trials, **info):
                if iteration == 1:
                    this.assertEqual(scheduled, len(trials))
                    this.assertEqual(
                        scheduled,
                        sum(
                            len(s) for s in
                            trial_executor._pg_manager._staging.values()) +
                        sum(
                            len(s)
                            for s in trial_executor._pg_manager._ready.values(
                            )) + len(trial_executor._pg_manager._in_use_pgs))

        start = time.time()
        out = tune.run(
            train,
            config={"start_time": start},
            resources_per_trial=placement_group_factory,
            num_samples=10,
            trial_executor=trial_executor,
            callbacks=[_TestCallback()])

        trial_end_times = sorted(t.last_result["end"] for t in out.trials)
        print("Trial end times:", trial_end_times)
        max_diff = trial_end_times[-1] - trial_end_times[0]

        # Not all trials have been run in parallel
        self.assertGreater(max_diff, 5)

        # Some trials should have run in parallel
        self.assertLess(max_diff, 10)

    @patch("ray.tune.trial_runner.TUNE_MAX_PENDING_TRIALS_PG", 6)
    @patch("ray.tune.utils.placement_groups.TUNE_MAX_PENDING_TRIALS_PG", 6)
    def testPlacementGroupLimitedRequests(self):
        """Assert that maximum number of placement groups is enforced."""
        self.testPlacementGroupRequests(scheduled=6)

    def testPlacementGroupDistributedTraining(self):
        """Run distributed training using placement groups.

        Each trial requests 4 CPUs and starts 4 remote training workers.
        """

        def placement_group_factory():
            head_bundle = {"CPU": 1, "GPU": 0, "custom": 0}
            child_bundle = {"CPU": 1}

            return placement_group(
                [head_bundle, child_bundle, child_bundle, child_bundle])

        @ray.remote
        class TrainingActor:
            def train(self, val):
                time.sleep(1)
                return val

        def train(config):
            base = config["base"]
            actors = [TrainingActor.remote() for _ in range(4)]
            futures = [
                actor.train.remote(base + 2 * i)
                for i, actor in enumerate(actors)
            ]
            results = ray.get(futures)

            end = time.time() - config["start_time"]
            tune.report(avg=np.mean(results), end=end)

        trial_executor = RayTrialExecutor()

        start = time.time()
        out = tune.run(
            train,
            config={
                "start_time": start,
                "base": tune.grid_search(list(range(0, 100, 10)))
            },
            resources_per_trial=placement_group_factory,
            num_samples=1,
            trial_executor=trial_executor)

        avgs = sorted(t.last_result["avg"] for t in out.trials)
        self.assertSequenceEqual(avgs, list(range(3, 103, 10)))

        trial_end_times = sorted(t.last_result["end"] for t in out.trials)
        print("Trial end times:", trial_end_times)
        max_diff = trial_end_times[-1] - trial_end_times[0]

        # Not all trials have been run in parallel
        self.assertGreater(max_diff, 5)

        # Some trials should have run in parallel
        # Todo: Re-enable when using buildkite
        # self.assertLess(max_diff, 10)

        # Assert proper cleanup
        pg_manager = trial_executor._pg_manager
        self.assertFalse(pg_manager._in_use_trials)
        self.assertFalse(pg_manager._in_use_pgs)
        self.assertFalse(pg_manager._staging_futures)
        for pgf in pg_manager._staging:
            self.assertFalse(pg_manager._staging[pgf])
        for pgf in pg_manager._ready:
            self.assertFalse(pg_manager._ready[pgf])
        self.assertTrue(pg_manager._latest_staging_start_time)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main(["-v", __file__]))
