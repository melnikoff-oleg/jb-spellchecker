from abc import ABC
from typing import List, Iterable, Tuple

import torch
from transformers import GPT2Tokenizer

from grazie.spell.main.model.generating.beam_search import BeamSearch
from grazie.spell.main.model.generating.info import GenerationInfo
from grazie.spell.main.model.generating.model import GenerationModel
# from grazie.spell.main.model.generating.prefix_match import FuzzyPrefixMatcher
from grazie.spell.main.model.generating.search import Search


class CompletionGeneration(ABC):
    def __init__(self, model: GenerationModel, tokenizer: GPT2Tokenizer,
                 # prefix_err_limit: int = 0
                 ):
        self.model = model
        self.tokenizer = tokenizer
        self.tokens_by_id = [tokenizer.decode(token_id) for token_id in range(tokenizer.vocab_size)]
        # self.prefix_err_limit = prefix_err_limit
        # self.prefix_matcher = FuzzyPrefixMatcher(tokenizer, self.prefix_err_limit, min_token_prefix_len=3)

        self.vocab_size = self.tokenizer.vocab_size
        self._verbose = False

        self._contexts: torch.Tensor
        self._gen_state: GenerationModel.GenerationState
        self._each_step_probs: torch.Tensor
        # self._prefixes: List[Tuple[str, int]]
        self._next_log_probs: torch.Tensor

    def modify_score(self, scores: torch.Tensor):
        if scores.shape[1] > self.vocab_size:
            scores = scores[:, :self.vocab_size]

        # prefix
        # for i, (prefix, err_limit) in enumerate(self._prefixes):
        #     if prefix != '':
        #         not_matched, matched_by_err_count = self.prefix_matcher.prefix_tokens_by_err(prefix, err_limit)
        #         scores[i, not_matched] = -float("inf")
        #         # if len(prefix_inds_by_err[1]) > 0:
        #         #     max_strict_len = max([len(self.tokenizer.decode(id)) for id in prefix_inds_by_err[1]])
        #         for err_num, prefix_token in enumerate(matched_by_err_count):
        #             if err_num != 0:
        #                 scores[i, prefix_token] = scores[i, prefix_token] + err_num * self.log_spell_prob

        # normalized prefix probs
        # if modified:
        #     for i, _ in enumerate(self._prefixes):
        #         probs = torch.exp(scores[i])
        #         scores[i] = torch.log(probs / probs.sum())

        return scores

    def init_state(self, context: torch.Tensor, prefix: str):
        self._contexts = context
        self._gen_state = self.model.create_state()
        self._each_step_probs = torch.empty(1, 0, dtype=torch.float, device=context.device)
        # self._prefixes = [(prefix, self.prefix_err_limit) if len(prefix) >= 3 else (prefix, 0)]

    def _sort_state(self, sort_mask: torch.Tensor) -> None:
        self._contexts = self._contexts[sort_mask]
        self._each_step_probs = self._each_step_probs[sort_mask]
        self._gen_state.update(sort_mask)
        # self._prefixes = [self._prefixes[i] for i in sort_mask.tolist()]

    # def _update_prefix(self, new_tokens_ids: torch.Tensor):
    #     result = []
    #     for (prefix, err_limit), token_id in zip(self._prefixes, new_tokens_ids.tolist()):
    #         token = self.prefix_matcher.tokens_by_id[token_id]
    #         err_cnt = self.prefix_matcher.levenshtein_dist(prefix, token)
    #         new_prefix = prefix[len(token):]
    #         result.append((new_prefix, min(err_limit - err_cnt, len(new_prefix))))
    #     self._prefixes = result

    def update_state(self, sort_mask: torch.Tensor, new_ids: torch.Tensor) -> None:
        self._sort_state(sort_mask)

        self._contexts = torch.cat((self._contexts, new_ids.unsqueeze(1)), 1)
        last_tokens_probs = torch.exp(self._next_log_probs[sort_mask, new_ids]).unsqueeze(1)
        self._each_step_probs = torch.cat((self._each_step_probs, last_tokens_probs), 1)

        # self._update_prefix(new_ids)

    def update_scores(self) -> torch.Tensor:
        with torch.no_grad():
            scores = self.model.next_probs(self._contexts, self._gen_state)
            scores = torch.log(scores)

            self._next_log_probs = self.modify_score(scores)
            return scores

    def is_end_of_words(self) -> List[bool]:
        end_of_words = []
        _, tokens_ids = torch.topk(self._next_log_probs, 3, dim=1, largest=True,
                                   sorted=True)  # both (batch_size * num_beams, 3)

        # for batch_id, (token_ids, (prefix, err_limit)) in enumerate(zip(tokens_ids, self._prefixes)):
        for batch_id, token_ids in enumerate(tokens_ids):
            tokens = [self.tokens_by_id[token_id] for token_id in token_ids]
            is_end_of_word = any([not token[0].isalpha() for token in tokens])
            end_of_words.append(is_end_of_word)

        return end_of_words

    def current_hypothesis(self, search: Search, mask: List[bool] = None) -> List[GenerationInfo]:
        if mask is None:
            mask = [True for _ in search.hypotheses]
        ans = sorted([
            GenerationInfo(probs.cpu().tolist(), score=score.item(), ids=hyp.tolist())
            for hyp, probs, score, is_ended in zip(search.hypotheses, self._each_step_probs, search.scores, mask)
            if is_ended
        ], key=lambda x: x.ids)
        return ans

    def generate(self, context: torch.Tensor, prefix: str, num_beams: int, num_iterations: int, min_len: int = 1,
                 repetition_penalty: float = 1.0, **kwargs) -> Iterable[List[GenerationInfo]]:
        search = BeamSearch(self.vocab_size, num_beams, repetition_penalty)
        self.init_state(context, prefix)
        self.update_scores()
        for iter_num in range(num_iterations):
            sort_mask, new_tokens = search.step(self._next_log_probs, context)
            self.update_state(sort_mask, new_tokens)
            self.update_scores()
            if iter_num + 1 >= min_len:
                yield self.current_hypothesis(search, self.is_end_of_words())
