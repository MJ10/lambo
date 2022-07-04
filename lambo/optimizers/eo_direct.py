import numpy as np
import torch
import hydra
import wandb
import time
import pandas as pd

from botorch.utils.multi_objective import pareto, infer_reference_point

from pymoo.factory import get_termination, get_performance_indicator
from pymoo.optimize import minimize
from itertools import product

from lambo.tasks.surrogate_task import SurrogateTask
from lambo.models.lm_elements import LanguageModel
from lambo.utils import weighted_resampling, DataSplit, update_splits, safe_np_cat
from lambo.metrics.r2 import r2_indicator_set
from lambo.metrics.hsr_indicator import HSR_Calculator

def pareto_frontier(candidate_pool, obj_vals, maximize=False):
    """
    args:
        candidate_pool: NumPy array of candidate objects
        obj_vals: NumPy array of objective values (assumes minimization)
    """
    assert len(candidate_pool) == obj_vals.shape[0]
    if len(candidate_pool) == 1:
        return candidate_pool, obj_vals
    # pareto utility assumes maximization
    if maximize:
        pareto_mask = pareto.is_non_dominated(torch.tensor(obj_vals))
    else:
        pareto_mask = pareto.is_non_dominated(-torch.tensor(obj_vals))
    return candidate_pool[pareto_mask], obj_vals[pareto_mask]

def generate_simplex(dims, n_per_dim):
    spaces = [np.linspace(0.0, 1.0, n_per_dim) for _ in range(dims)]
    return np.array([comb for comb in product(*spaces) 
                     if np.allclose(sum(comb), 1.0)])

def thermometer(v, n_bins=50, vmin=0, vmax=32):
    bins = torch.linspace(vmin, vmax, n_bins)
    gap = bins[1] - bins[0]
    return (v[..., None] - bins.reshape((1,) * v.ndim + (-1,))).clamp(0, gap.item()) / gap

class Normalizer(object):
    def __init__(self, loc=0., scale=1.):
        self.loc = loc
        self.scale = np.where(scale != 0, scale, 1.)

    def __call__(self, arr):
        min_val = self.loc - 4 * self.scale
        max_val = self.loc + 4 * self.scale
        clipped_arr = np.clip(arr, a_min=min_val, a_max=max_val)
        norm_arr = (clipped_arr - self.loc) / self.scale

        return norm_arr

    def inv_transform(self, arr):
        return self.scale * arr + self.loc

class SequentialGeneticOptimizer(object):
    def __init__(self, bb_task, algorithm, tokenizer, num_rounds, num_gens, seed, concentrate_pool=1,
                 residue_sampler='uniform', resampling_weight=1., **kwargs):
        self.bb_task = bb_task
        self.algorithm = algorithm
        self.num_rounds = num_rounds
        self.num_gens = num_gens
        self.term_fn = get_termination("n_gen", num_gens)
        self.seed = seed
        self.concentrate_pool = concentrate_pool
        self.residue_sampler = residue_sampler

        tokenizer.set_sampling_vocab(None, bb_task.max_ngram_size)
        self.tokenizer = tokenizer

        self.encoder = None

        self._hv_ref = None
        self._ref_point = np.array([1] * self.bb_task.obj_dim)
        self.simplex_bins = kwargs["simplex_bins"]
        self.active_candidates = None
        self.active_targets = None
        self.resampling_weight = resampling_weight

    def optimize(self, candidate_pool, pool_targets, all_seqs, all_targets, log_prefix=''):
        batch_size = self.bb_task.batch_size
        import pdb; pdb.set_trace();
        # all_targets = -all_targets
        bb_task = hydra.utils.instantiate(self.bb_task, tokenizer=self.tokenizer, candidate_pool=candidate_pool,
                                          batch_size=1)
        self.num_props = bb_task.obj_dim
        self.simplex = generate_simplex(self.num_props, self.simplex_bins)
        is_feasible = bb_task.is_feasible(candidate_pool)
        pool_candidates = candidate_pool[is_feasible]
        pool_targets = pool_targets[is_feasible]
        pool_seqs = np.array([p_cand.mutant_residue_seq for p_cand in pool_candidates])

        self.all_seqs = all_seqs
        self.all_targets = all_targets
        new_seqs = all_seqs.copy()
        new_targets = all_targets.copy()
        self.active_candidates, self.active_targets = pool_candidates, pool_targets
        self.active_seqs = pool_seqs

        pareto_candidates, pareto_targets = pareto_frontier(self.active_candidates, self.active_targets, maximize=True)
        self.pareto_seqs = np.array([p_cand.mutant_residue_seq for p_cand in pareto_candidates])
        pareto_cand_history = pareto_candidates.copy()
        pareto_seq_history = self.pareto_seqs.copy()
        pareto_target_history = pareto_targets.copy()
        norm_pareto_targets = pareto_targets
        self._ref_point = np.zeros(self.bb_task.obj_dim)
        rescaled_ref_point = self._ref_point
        # rescaled_ref_point = hypercube_transform.inv_transform(self._ref_point)

        # logging setup
        total_bb_evals = 0
        start_time = time.time()
        round_idx = 0
        self._log_candidates(pareto_candidates, pareto_targets, round_idx, log_prefix)
        metrics = self._log_optimizer_metrics(norm_pareto_targets, round_idx, total_bb_evals, start_time, log_prefix)

        print('\n best candidates')
        obj_vals = {f'obj_val_{i}': pareto_targets[:, i].min() for i in range(self.bb_task.obj_dim)}
        print(pd.DataFrame([obj_vals]).to_markdown(floatfmt='.4f'))

        # set up encoder which may also be a masked language model (MLM)
        encoder = None if self.encoder is None else hydra.utils.instantiate(
            self.encoder, tokenizer=self.tokenizer
        )

        if self.residue_sampler == 'uniform':
            mlm_obj = None
        elif self.residue_sampler == 'mlm':
            assert isinstance(encoder, LanguageModel)
            mlm_obj = encoder
        else:
            raise ValueError

        for round_idx in range(1, self.num_rounds + 1):
            # contract active pool to current Pareto frontier
            if self.concentrate_pool > 0 and round_idx % self.concentrate_pool == 0:
                self.active_candidates, self.active_targets = pareto_frontier(self.active_candidates, self.active_targets)
                self.active_seqs = np.array([a_cand.mutant_residue_seq for a_cand in self.active_candidates])
                print(f'\nactive set contracted to {self.active_candidates.shape[0]} pareto points')
            # augment active set with old pareto points
            if self.active_candidates.shape[0] < batch_size:
                num_samples = min(batch_size, pareto_cand_history.shape[0])
                num_backtrack = min(num_samples, batch_size - self.active_candidates.shape[0])
                _, weights, _ = weighted_resampling(pareto_target_history, k=self.resampling_weight)
                hist_idxs = np.random.choice(
                    np.arange(pareto_cand_history.shape[0]), num_samples, p=weights, replace=False
                )
                is_active = np.in1d(pareto_seq_history[hist_idxs], self.active_seqs)
                hist_idxs = hist_idxs[~is_active]
                if hist_idxs.size > 0:
                    hist_idxs = hist_idxs[:num_backtrack]
                    backtrack_candidates = pareto_cand_history[hist_idxs]
                    backtrack_targets = pareto_target_history[hist_idxs]
                    backtrack_seqs = pareto_seq_history[hist_idxs]
                    self.active_candidates = np.concatenate((self.active_candidates, backtrack_candidates))
                    self.active_targets = np.concatenate((self.active_targets, backtrack_targets))
                    self.active_seqs = np.concatenate((self.active_seqs, backtrack_seqs))
                    print(f'active set augmented with {backtrack_candidates.shape[0]} backtrack points')
            # augment active set with random points
            if self.active_candidates.shape[0] < batch_size:
                num_samples = min(batch_size, pool_candidates.shape[0])
                num_rand = min(num_samples, batch_size - self.active_candidates.shape[0])
                _, weights, _ = weighted_resampling(pool_targets, k=self.resampling_weight)
                rand_idxs = np.random.choice(
                    np.arange(pool_candidates.shape[0]), num_samples, p=weights, replace=False
                )
                is_active = np.in1d(pool_seqs[rand_idxs], self.active_seqs)
                rand_idxs = rand_idxs[~is_active][:num_rand]
                rand_candidates = pool_candidates[rand_idxs]
                rand_targets = pool_targets[rand_idxs]
                rand_seqs = pool_seqs[rand_idxs]
                self.active_candidates = np.concatenate((self.active_candidates, rand_candidates))
                self.active_targets = np.concatenate((self.active_targets, rand_targets))
                self.active_seqs = np.concatenate((self.active_seqs, rand_seqs))
                print(f'active set augmented with {rand_candidates.shape[0]} random points')

            if self.resampling_weight is None:
                active_weights = np.ones(self.active_targets.shape[0]) / self.active_targets.shape[0]
            else:
                _, active_weights, _ = weighted_resampling(self.active_targets, k=self.resampling_weight)

            # prepare the inner task
            # z_score_transform = Normalizer(self.all_targets.mean(0), self.all_targets.std(0))

            # algorithm setup
            algorithm = hydra.utils.instantiate(self.algorithm)
            algorithm.initialization.sampling.tokenizer = self.tokenizer
            algorithm.mating.mutation.tokenizer = self.tokenizer

            if not self.residue_sampler == 'uniform':
                algorithm.initialization.sampling.mlm_obj = mlm_obj
                algorithm.mating.mutation.mlm_obj = mlm_obj

            problem = self._create_inner_task(
                candidate_pool=self.active_candidates,
                candidate_weights=active_weights,
                input_data=new_seqs,
                target_data=new_targets,
                ref_point=rescaled_ref_point,
                encoder=encoder,
                round_idx=round_idx,
                num_bb_evals=total_bb_evals,
                start_time=start_time,
                log_prefix=log_prefix,
            )

            print('---- optimizing candidates ----')
            res = minimize(
                problem,
                algorithm,
                self.term_fn,
                save_history=False,
                verbose=True
            )

            # query outer task, append data
            new_candidates, new_targets, new_seqs, bb_evals = self._evaluate_result(
                res, self.active_candidates, round_idx, total_bb_evals, start_time, log_prefix
            )
            total_bb_evals += bb_evals

            # filter infeasible candidates
            is_feasible = bb_task.is_feasible(new_candidates)
            new_seqs = new_seqs[is_feasible]
            new_candidates = new_candidates[is_feasible]
            new_targets = new_targets[is_feasible]
            if new_candidates.size == 0:
                print('no new candidates')
                continue

            # filter duplicate candidates
            new_seqs, unique_idxs = np.unique(new_seqs, return_index=True)
            new_candidates = new_candidates[unique_idxs]
            new_targets = new_targets[unique_idxs]

            # filter redundant candidates
            is_new = np.in1d(new_seqs, self.all_seqs, invert=True)
            new_seqs = new_seqs[is_new]
            new_candidates = new_candidates[is_new]
            new_targets = new_targets[is_new]
            if new_candidates.size == 0:
                print('no new candidates')
                self._log_optimizer_metrics(
                    norm_pareto_targets, round_idx, total_bb_evals, start_time, log_prefix
                )
                continue

            pool_candidates = np.concatenate((pool_candidates, new_candidates))
            pool_targets = np.concatenate((pool_targets, new_targets))
            pool_seqs = np.concatenate((pool_seqs, new_seqs))

            self.all_seqs = np.concatenate((self.all_seqs, new_seqs))
            self.all_targets = np.concatenate((self.all_targets, new_targets))

            for seq in new_seqs:
                if hasattr(self.tokenizer, 'to_smiles'):
                    print(self.tokenizer.to_smiles(seq))
                else:
                    print(seq)

            # augment active pool with candidates that can be mutated again
            self.active_candidates = np.concatenate((self.active_candidates, new_candidates))
            self.active_targets = np.concatenate((self.active_targets, new_targets))
            self.active_seqs = np.concatenate((self.active_seqs, new_seqs))

            # overall Pareto frontier including terminal candidates
            pareto_candidates, pareto_targets = pareto_frontier(
                np.concatenate((pareto_candidates, new_candidates)),
                np.concatenate((pareto_targets, new_targets)),
            )
            self.pareto_seqs = np.array([p_cand.mutant_residue_seq for p_cand in pareto_candidates])

            print(new_targets)
            print('\n new candidates')
            obj_vals = {f'obj_val_{i}': new_targets[:, i].min() for i in range(self.bb_task.obj_dim)}
            print(pd.DataFrame([obj_vals]).to_markdown(floatfmt='.4f'))

            print('\n best candidates')
            obj_vals = {f'obj_val_{i}': pareto_targets[:, i].min() for i in range(self.bb_task.obj_dim)}
            print(pd.DataFrame([obj_vals]).to_markdown(floatfmt='.4f'))

            par_is_new = np.in1d(self.pareto_seqs, pareto_seq_history, invert=True)
            pareto_cand_history = safe_np_cat([pareto_cand_history, pareto_candidates[par_is_new]])
            pareto_seq_history = safe_np_cat([pareto_seq_history, self.pareto_seqs[par_is_new]])
            pareto_target_history = safe_np_cat([pareto_target_history, pareto_targets[par_is_new]])

            # logging
            norm_pareto_targets = pareto_targets
            self._log_candidates(new_candidates, new_targets, round_idx, log_prefix)
            metrics = self._log_optimizer_metrics(norm_pareto_targets, round_idx, total_bb_evals, start_time, log_prefix)

        return metrics

    def _evaluate_result(self, *args, **kwargs):
        raise NotImplementedError

    def _create_inner_task(self, *args, **kwargs):
        raise NotImplementedError

    def compute_mo_metrics(self, solutions):
        hv_indicator = get_performance_indicator('hv', ref_point=self._ref_point)
        # print(pareto_targets)
        hv = hv_indicator.do(-solutions)
        
        r2 = r2_indicator_set(self.simplex, solutions, np.ones(self.num_props))
        hsr_class = HSR_Calculator(lower_bound=-np.ones(self.num_props), upper_bound=np.zeros(self.num_props))
        hsri, x = hsr_class.calculate_hsr(-solutions)
        
        return hv, r2, hsri

    def _log_candidates(self, candidates, targets, round_idx, log_prefix):
        table_cols = ['round_idx', 'cand_uuid', 'cand_ancestor', 'cand_seq']
        table_cols.extend([f'obj_val_{idx}' for idx in range(self.bb_task.obj_dim)])
        for cand, obj in zip(candidates, targets):
            new_row = [round_idx, cand.uuid, cand.wild_name, cand.mutant_residue_seq]
            new_row.extend([elem for elem in obj])
            record = {'/'.join((log_prefix, 'candidates', key)): val for key, val in zip(table_cols, new_row)}
            wandb.log(record)

    def _log_optimizer_metrics(self, normed_targets, round_idx, num_bb_evals, start_time, log_prefix):
        hv_indicator = get_performance_indicator('hv', ref_point=self._ref_point)
        # new_hypervol = hv_indicator.do(normed_targets)
        mo_metrics = self.compute_mo_metrics(normed_targets)
        self._hv_ref = mo_metrics[0] if self._hv_ref is None else self._hv_ref
        metrics = dict(
            round_idx=round_idx,
            hv=mo_metrics[0],
            r2=mo_metrics[1],
            hsri=mo_metrics[2],
            hypervol_rel=mo_metrics[0] / max(1e-6, self._hv_ref),
            num_bb_evals=num_bb_evals,
            time_elapsed=time.time() - start_time,
        )
        print(pd.DataFrame([metrics]).to_markdown())
        metrics = {'/'.join((log_prefix, 'opt_metrics', key)): val for key, val in metrics.items()}
        wandb.log(metrics)
        return metrics

class ModelFreeGeneticOptimizer(SequentialGeneticOptimizer):
    def _create_inner_task(
            self, candidate_pool, input_data, target_data, candidate_weights, *args, **kwargs):
        inner_task = hydra.utils.instantiate(
            self.bb_task,
            candidate_pool=candidate_pool,
            tokenizer=self.tokenizer,
            batch_size=1,
            candidate_weights=candidate_weights,
        )
        return inner_task

    def _evaluate_result(self, result, candidate_pool, *args, **kwargs):
        new_candidates = result.pop.get('X_cand').reshape(-1)
        new_seqs = result.pop.get('X_seq').reshape(-1)
        new_targets = result.pop.get('F')
        bb_evals = self.num_gens * self.algorithm.pop_size
        return new_candidates, new_targets, new_seqs, bb_evals