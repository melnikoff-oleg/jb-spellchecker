import random
import string

from transformers import RobertaTokenizer
import time
from transformers import BartConfig
import transformers
from model.detector import *
from model.candidator import *
from model.ranker import *

PATH_PREFIX = '/home/ubuntu/omelnikov/spellchecker/'


class SpellCheckModelBase(ABC):

    @abstractmethod
    def correct(self, text: str) -> str:
        raise NotImplementedError

    def correct_strings(self, texts: List[str]) -> List[str]:
        return [self.correct(text) for text in texts]

    def correct_from_file(self, src: str, dest: str):
        with open(src) as src_texts:
            with open(dest) as dest_texts:
                for text in src_texts:
                    dest_texts.write(self.correct(text[:-1]) + '\n')


class OldBartChecker(SpellCheckModelBase):
    def __init__(self, checkpoint: str = 'No learning', model: BartForConditionalGeneration = None,
                 device: torch.device = None, tokenizer: RobertaTokenizer = None):
        self.checkpoint = checkpoint
        transformers.set_seed(42)
        if tokenizer is None:
            self.tokenizer = BartTokenizer.from_pretrained('facebook/bart-base')
        else:
            self.tokenizer = tokenizer
        self.detector = HunspellDetector()
        if device is None:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        else:
            self.device = device

        if model is not None:
            self.model = model
        else:
            if checkpoint == 'No learning':
                self.model = BartForConditionalGeneration.from_pretrained('facebook/bart-base')
            else:
                config = BartForConditionalGeneration.from_pretrained('facebook/bart-base').config
                self.model = BartForConditionalGeneration(config)
                # Model was trained on GPU, maybe we are inferring on CPU
                if self.device == torch.device('cpu'):
                    self.model.load_state_dict(torch.load(checkpoint, map_location='cpu'))
                else:
                    self.model.load_state_dict(torch.load(checkpoint))

        self.model = self.model.to(self.device)

    def correct(self, text: str) -> str:

        # CAPS handling
        caps = (text.upper() == text)
        if caps:
            text = text.lower()

        # no dot at the end handle
        add_dot = is_needed_to_add_dot_to_end(text)
        if add_dot:
            text += '.'

        spells = self.detector.detect(text)

        shift = 0
        for spell in spells:
            text = text[: shift + spell.interval[0]] + '<mask>' + text[shift + spell.interval[1]:]
            shift += 6 - len(spell.word)

        ans_ids = self.model.generate(self.tokenizer([text], return_tensors='pt').to(self.device)["input_ids"],
                                      num_beams=5, min_length=5, max_length=500)
        ans_tokens = self.tokenizer.batch_decode(ans_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
        text = ' '.join(ans_tokens)

        if add_dot:
            text = text[:-1]

        if caps:
            text = text.upper()

        return text


class DCR(SpellCheckModelBase):
    def __init__(self):
        self.detector: BaseDetector = HunspellDetector()
        self.candidator: BaseCandidator = HunspellCandidator()

        checkpoint_path = PATH_PREFIX + 'training/checkpoints/bart-sep-mask-all-sent-distil-dec05_v0_81396.pt'
        config = BartConfig(vocab_size=50265, max_position_embeddings=1024, encoder_layers=6, encoder_ffn_dim=3072,
                                encoder_attention_heads=12, decoder_layers=3, decoder_ffn_dim=3072,
                                decoder_attention_heads=12, encoder_layerdrop=0.0, decoder_layerdrop=0.0,
                                activation_function='gelu', d_model=768, dropout=0.1, attention_dropout=0.0,
                                activation_dropout=0.0, init_std=0.02, classifier_dropout=0.0, scale_embedding=False,
                                use_cache=True, num_labels=3, pad_token_id=1, bos_token_id=0, eos_token_id=2,
                                is_encoder_decoder=True, decoder_start_token_id=2, forced_eos_token_id=2)
        model = BartForConditionalGeneration(config)
        model.load_state_dict(torch.load(checkpoint_path))
        self.device = torch.device('cuda')
        model = model.to(self.device)
        model.eval()
        self.ranker_tokenizer = BartTokenizer.from_pretrained('facebook/bart-base')
        self.ranker_model: BartForConditionalGeneration = model

    def from_pretrained(self):
        self.ranker_model = BartForConditionalGeneration.from_pretrained('melnikoff-oleg/distilbart-sep-mask-all')
        self.ranker_model.to(self.device)
        self.ranker_tokenizer = BartTokenizer.from_pretrained('melnikoff-oleg/distilbart-sep-mask-all')

    def correct(self, text: str, return_all_stages: bool = False) -> str:

        caps = (text.upper() == text)
        if caps:
            text = text.lower()

        spelled_words = self.detector.detect(text)
        candidates = self.candidator.get_candidates(text, spelled_words)

        _spelled_words, _candidates = [], []
        for idx, (spelled_word, cands) in enumerate(zip(spelled_words, candidates)):
            if len(candidates[idx]) > 0:
                _spelled_words.append(spelled_word)
                _candidates.append(cands)
        spelled_words, candidates = _spelled_words, _candidates

        texts = []
        outs = []
        texts_inds = []
        cands_ranges = []
        for i, (spelled_word, cands) in enumerate(zip(spelled_words, candidates)):
            text, start, finish = spelled_word.text, spelled_word.interval[0], spelled_word.interval[1]
            text_pref = text[: start]
            text_suff = text[finish:]
            if (start == 0 or text[start - 1] == ' ') and (finish == len(text) or not text[finish].isalpha()):
                texts_inds += [i for _ in cands]

                input_texts = [spelled_word.word + ' </s> ' + text_pref + '<mask>' + text_suff for _ in cands]
                output_texts = [text_pref + syn + text_suff for syn in cands]

                texts += input_texts
                outs += output_texts
                cands_ranges += [(len(self.ranker_tokenizer.encode(text_pref[:-1])),
                                  len(self.ranker_tokenizer.encode(syn, add_special_tokens=False))) for syn in cands]
            else:
                print('Error with SpelledWord')
                print('SpelledWord:', spelled_word)
                print('Candidates:', cands)
                raise Exception

        batch_size = 16

        scores: List[List[float]] = [[] for _ in spelled_words]

        for start in range(0, len(texts), batch_size):
            end = min(start + batch_size, len(texts))

            encoded_input = self.ranker_tokenizer(texts[start: end], return_tensors='pt', truncation=True,
                                           padding=True).to(self.device)['input_ids']
            encoded_output = self.ranker_tokenizer(outs[start: end], return_tensors='pt', truncation=True,
                                            padding=True).to(self.device)['input_ids']

            # BART eval
            all_logits = self.ranker_model(encoded_input, labels=encoded_output).logits.cpu()

            for i, logits in enumerate(all_logits):
                ind = texts_inds[start + i]
                syn_range = cands_ranges[start + i]
                word_logits = logits[syn_range[0] - 1: syn_range[0] + syn_range[1] - 1]
                log_probs = torch.log_softmax(word_logits, dim=1)
                word_log_prob = torch.tensor(0.0)
                for j, token_idx in enumerate(encoded_output[i][syn_range[0] - 1: syn_range[0] + syn_range[1] - 1]):
                    word_log_prob += log_probs[j, token_idx]
                scores[ind].append(word_log_prob.item())

        result: List[str] = ['' for _ in spelled_words]

        for i, cur_scores in enumerate(scores):
            mx = -1e18
            mx_ind = None
            for j, score in enumerate(cur_scores):
                if mx < score:
                    mx = score
                    mx_ind = j
            result[i] = candidates[i][mx_ind]

        corrections = result

        shift = 0
        res_text = text
        for i, spelled_word in enumerate(spelled_words):
            res_text = res_text[: shift + spelled_word.interval[0]] + corrections[i] + \
                       res_text[shift + spelled_word.interval[1]:]
            shift += len(corrections[i]) - len(spelled_word.word)

        if caps:
            res_text = res_text.upper()

        if return_all_stages:
            return res_text, spelled_words, candidates, corrections
        else:
            return res_text


class DetectorCandidatorRanker(SpellCheckModelBase):

    def __init__(self):
        self.detector: BaseDetector = HunspellDetector()
        self.candidator: BaseCandidator = HunspellCandidator()
        # self.ranker: BaseRanker = BartRanker()
        # config = BartConfig(vocab_size=50265, max_position_embeddings=1024, encoder_layers=6, encoder_ffn_dim=3072,
        #                     encoder_attention_heads=12, decoder_layers=3, decoder_ffn_dim=3072,
        #                     decoder_attention_heads=12, encoder_layerdrop=0.0, decoder_layerdrop=0.0,
        #                     activation_function='gelu', d_model=768, dropout=0.1, attention_dropout=0.0,
        #                     activation_dropout=0.0, init_std=0.02, classifier_dropout=0.0, scale_embedding=False,
        #                     use_cache=True, num_labels=3, pad_token_id=1, bos_token_id=0, eos_token_id=2,
        #                     is_encoder_decoder=True, decoder_start_token_id=2, forced_eos_token_id=2)
        # self.ranker: BaseRanker = BartSepMaskAllRanker(
        # checkpoint_path=PATH_PREFIX + 'training/checkpoints/bart-sep-mask-all-sent-distil-dec05_v0_248088.pt',
        # config=config,
        # device=torch.device('cuda'))
        # self.ranker = BartSepMaskAllRanker(
        #     checkpoint_path=PATH_PREFIX + 'training/checkpoints/bart-sep-mask_v1_3.pt',
        #     device=torch.device('cuda'))
        # self.ranker = BartSepMaskAllRanker(
        #     checkpoint_path=PATH_PREFIX + 'training/checkpoints/bart-sep-mask-all-sent_v0_1236504.pt',
        #     device=torch.device('cuda'))
        # self.ranker: BaseRanker = BartFineTuneRanker()
        self.ranker: BaseRanker = LogisticRegressionMetaRanker()

    def correct(self, text: str, return_all_stages: bool = False):

        # DEBUG
        print(f'Text: {text}')

        caps = (text.upper() == text)
        if caps:
            text = text.lower()

        spelled_words = self.detector.detect(text)

        # DEBUG
        print(f'Detections: {spelled_words}')

        candidates = self.candidator.get_candidates(text, spelled_words)

        # DEBUG
        print(f'Candidates: {candidates}')

        _spelled_words, _candidates = [], []
        for idx, (spelled_word, cands) in enumerate(zip(spelled_words, candidates)):
            if len(candidates[idx]) > 0:
                _spelled_words.append(spelled_word)
                _candidates.append(cands)
        spelled_words, candidates = _spelled_words, _candidates

        corrections = self.ranker.rank(text, spelled_words, candidates) if len(candidates) > 0 else []

        # DEBUG
        print(f'Corrections: {corrections}')

        shift = 0
        res_text = text
        for i, spelled_word in enumerate(spelled_words):
            res_text = res_text[: shift + spelled_word.interval[0]] + corrections[i] + res_text[shift + spelled_word.interval[1]: ]
            shift += len(corrections[i]) - len(spelled_word.word)

        if caps:
            res_text = res_text.upper()

        if return_all_stages:
            return res_text, spelled_words, candidates, corrections
        else:
            return res_text


class CharBasedTransformerChecker(SpellCheckModelBase):

    class BartTokenizer(RobertaTokenizer):
        vocab_files_names = {"vocab_file": PATH_PREFIX + "data_utils/char_based_transformer_vocab/vocab.json",
                             "merges_file": PATH_PREFIX + "data_utils/char_based_transformer_vocab/merges.txt"}

    def __init__(self, config, checkpoint: str = 'No learning', model: BartForConditionalGeneration = None,
                 device: torch.device = None):
        self.checkpoint = checkpoint
        transformers.set_seed(42)
        self.tokenizer = CharBasedTransformerChecker.BartTokenizer(
            PATH_PREFIX + "data_utils/char_based_transformer_vocab/url_vocab.json",
            PATH_PREFIX + "data_utils/char_based_transformer_vocab/url_merges.txt"
        )
        if device is None:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        else:
            self.device = device

        if model is not None:
            self.model = model
        else:
            model_config = config
            self.model = BartForConditionalGeneration(model_config)
            if checkpoint != 'No learning':
                # Model was trained on GPU, maybe we are inferring on CPU
                if self.device == torch.device('cpu'):
                    self.model.load_state_dict(torch.load(checkpoint, map_location='cpu'))
                else:
                    self.model.load_state_dict(torch.load(checkpoint))

        self.model = self.model.to(self.device)

    def __str__(self):
        return f'Char-Based Transformer, checkpoint: {self.checkpoint.split("/")[-1]}'

    def correct(self, text: str) -> str:
        text = text.replace(' ', '_')
        ans_ids = self.model.generate(self.tokenizer([text], return_tensors='pt').to(self.device)["input_ids"],
                                      num_beams=5, min_length=5, max_length=500)
        ans_tokens = self.tokenizer.batch_decode(ans_ids, skip_special_tokens=False, clean_up_tokenization_spaces=False)
        for ind, i in enumerate(ans_tokens):
            ans_tokens[ind] = ans_tokens[ind].replace('_', ' ')[7:].split('<')[0]
        return ' '.join(ans_tokens)


class SpellCheckModelNeuSpell(SpellCheckModelBase):

    def __init__(self):
        with open(PATH_PREFIX + 'dataset/bea/bea60k.noise') as x:
            p = x.readlines()
        with open(PATH_PREFIX + 'experiments/neuspell-bert/bea60kdetok') as y:
            q = y.readlines()

        self.ans = {}

        for i, j in zip(p, q):
            self.ans[i[:-1]] = j[:-1]

    def correct(self, text: str) -> str:
        try:
            return self.ans[text]
        except Exception:

            print(f'Text "{text}" not found in NeuSpell result file')
            time.sleep(5)
            return '--BUG--'


class BartChecker(SpellCheckModelBase):

    def __init__(self, checkpoint: str = 'No learning', model: BartForConditionalGeneration = None,
                 device: torch.device = None):
        self.checkpoint = checkpoint
        transformers.set_seed(42)
        self.tokenizer = BartTokenizer.from_pretrained('facebook/bart-base')
        if device is None:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        else:
            self.device = device

        if model is not None:
            self.model = model
        else:
            if checkpoint == 'No learning':
                self.model = BartForConditionalGeneration.from_pretrained('facebook/bart-base')
            else:
                config = BartForConditionalGeneration.from_pretrained('facebook/bart-base').config
                self.model = BartForConditionalGeneration(config)
                # Model was trained on GPU, maybe we are inferring on CPU
                if self.device == torch.device('cpu'):
                    self.model.load_state_dict(torch.load(checkpoint, map_location='cpu'))
                else:
                    self.model.load_state_dict(torch.load(checkpoint, map_location=self.device))

        self.model = self.model.to(self.device)

    def from_pretrained(self):
        self.model = BartForConditionalGeneration.from_pretrained("melnikoff-oleg/bart-end-to-end")
        self.model.to(self.device)

    def __str__(self):
        return f'BART, checkpoint: {self.checkpoint.split("/")[-1]}'

    def correct(self, text: str) -> str:
        ans_ids = self.model.generate(self.tokenizer([text], return_tensors='pt').to(self.device)["input_ids"],
                                      num_beams=5, min_length=5, max_length=500)
        ans_tokens = self.tokenizer.batch_decode(ans_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
        return ' '.join(ans_tokens)


def is_needed_to_add_dot_to_end(s: string):
    if len(s) == 0:
        return False
    return not s[-1] in string.punctuation


class BartSepMaskAllChecker(SpellCheckModelBase):

    def __init__(self, checkpoint: str = 'No learning', model: BartForConditionalGeneration = None,
                 device: torch.device = None, tokenizer: RobertaTokenizer = None):
        self.checkpoint = checkpoint
        transformers.set_seed(42)
        if tokenizer is None:
            self.tokenizer = BartTokenizer.from_pretrained('facebook/bart-base')
        else:
            self.tokenizer = tokenizer
        self.detector = HunspellDetector()
        # self.detector = BERTDetector(threshold=0.65)
        if device is None:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        else:
            self.device = device

        if model is not None:
            self.model = model
        else:
            if checkpoint == 'No learning':
                self.model = BartForConditionalGeneration.from_pretrained('facebook/bart-base')
            else:
                config = BartForConditionalGeneration.from_pretrained('facebook/bart-base').config
                self.model = BartForConditionalGeneration(config)
                # Model was trained on GPU, maybe we are inferring on CPU
                if self.device == torch.device('cpu'):
                    self.model.load_state_dict(torch.load(checkpoint, map_location='cpu'))
                else:
                    self.model.load_state_dict(torch.load(checkpoint))

        self.model = self.model.to(self.device)

    def from_pretrained(self):
        self.model = BartForConditionalGeneration.from_pretrained('melnikoff-oleg/distilbart-sep-mask-all')
        self.model.to(self.device)
        self.tokenizer = BartTokenizer.from_pretrained('melnikoff-oleg/distilbart-sep-mask-all')

    def correct(self, text: str) -> str:

        # CAPS handling
        caps = (text.upper() == text)
        if caps:
            text = text.lower()

        # no dot at the end handle
        # add_dot = is_needed_to_add_dot_to_end(text)
        # if add_dot:
        #     text += '.'

        spells = self.detector.detect(text)

        # Надо подравить инференс на все токены
        shift = 0
        pref = ''
        for idx, spell in enumerate(spells):
            text = text[: shift + spell.interval[0]] + '<mask>' + text[shift + spell.interval[1]:]
            shift += 6 - len(spell.word)
            pref += spell.word
            if idx < len(spells) - 1:
                pref += ' </s> '
        text = pref + ' </s> ' + text
        # print('Input text:', text)
        # from time import sleep
        # sleep(5)

        # print('Tokenized text:', self.tokenizer([text], return_tensors='pt')["input_ids"])

        ans_ids = self.model.generate(self.tokenizer([text], return_tensors='pt').to(self.device)["input_ids"],
                                      num_beams=5, min_length=5, max_length=500)
        ans_tokens = self.tokenizer.batch_decode(ans_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
        text = ' '.join(ans_tokens)

        # print('Res text:', text)

        if caps:
            text = text.upper()

        return text


class CharBasedSepMask(SpellCheckModelBase):

    class BartTokenizer(RobertaTokenizer):
        vocab_files_names = {"vocab_file": PATH_PREFIX + "data_utils/char_based_transformer_vocab/vocab.json",
                             "merges_file": PATH_PREFIX + "data_utils/char_based_transformer_vocab/merges.txt"}

    def __init__(self, config: dict = None, checkpoint: str = 'No learning', model: BartForConditionalGeneration = None,
                 device: torch.device = None):
        self.checkpoint = checkpoint
        transformers.set_seed(42)
        self.tokenizer = CharBasedTransformerChecker.BartTokenizer(
            PATH_PREFIX + "data_utils/char_based_transformer_vocab/url_vocab.json",
            PATH_PREFIX + "data_utils/char_based_transformer_vocab/url_merges.txt"
        )
        if not config is None:
            config['vocab_size'] = self.tokenizer.vocab_size
        if device is None:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        else:
            self.device = device

        if model is not None:
            self.model = model
        else:
            model_config = BartConfig(**config)
            self.model = BartForConditionalGeneration(model_config)
            if checkpoint != 'No learning':
                # Model was trained on GPU, maybe we are inferring on CPU
                if self.device == torch.device('cpu'):
                    self.model.load_state_dict(torch.load(checkpoint, map_location='cpu'))
                else:
                    self.model.load_state_dict(torch.load(checkpoint))

        self.model = self.model.to(self.device)

    def __str__(self):
        return f'Char-Based Transformer, checkpoint: {self.checkpoint.split("/")[-1]}'

    def correct(self, text: str) -> str:
        init_text = text
        if text.upper() == text:
            text = text.lower()
        detector = HunspellDetector()
        # detector = BERTDetector()
        try:
            spells = detector.detect(text)
        except Exception as e:
            print(e)
            return text


        col = 0

        while len(spells) > 0 and col < 1:
            spell_ind = 0
            # spell_ind = random.randint(0, len(spells) - 1)
            new_text = spells[spell_ind].word + ' <sep> ' + text[: spells[spell_ind].interval[0]] + '<mask>'+ text[spells[spell_ind].interval[1]: ]
            if col > 0:
                print('Init tex:', init_text)
                print('Cur text:', text)
                print(f'New spell:|{spells[spell_ind].word}|')
            new_text = new_text.replace(' ', '_')
            ans_ids = self.model.generate(self.tokenizer([new_text], return_tensors='pt').to(self.device)["input_ids"],
                                      num_beams=5, min_length=5, max_length=500)
            ans_tokens = self.tokenizer.batch_decode(ans_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
            for ind, i in enumerate(ans_tokens):
                ans_tokens[ind] = ans_tokens[ind].replace('_', ' ')[7:].split('<')[0]
            text = ' '.join(ans_tokens)
            spells = detector.detect(text)
            col += 1

        if col > 1:
            print('Init text:', init_text)
            print('Cur text:', text)
            print()

        if init_text == init_text.upper():
            text = text.upper()
        return text


class MaskWordBartChecker(SpellCheckModelBase):

    def __init__(self, checkpoint: str = 'No learning', model: BartForConditionalGeneration = None,
                 device: torch.device = None):
        self.checkpoint = checkpoint
        transformers.set_seed(42)
        self.tokenizer = BartTokenizer.from_pretrained('facebook/bart-base')
        if device is None:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        else:
            self.device = device
        print('MaskWordBART device:', self.device)

        if model is not None:
            self.model = model
        else:
            if checkpoint == 'No learning':
                self.model = BartForConditionalGeneration.from_pretrained('facebook/bart-base')
            else:
                config = BartForConditionalGeneration.from_pretrained('facebook/bart-base').config
                self.model = BartForConditionalGeneration(config)
                self.model = self.model.to(self.device)
                # Model was trained on GPU, maybe we are inferring on CPU
                if self.device == torch.device('cpu'):
                    self.model.load_state_dict(torch.load(checkpoint, map_location='cpu'))
                else:
                    self.model.load_state_dict(torch.load(checkpoint, map_location=self.device))

        self.model = self.model.to(self.device)

    def __str__(self):
        return f'BART, checkpoint: {self.checkpoint.split("/")[-1]}'

    def correct(self, text: str) -> str:

        detector = HunspellDetector()
        # detector = BERTDetector()

        spells = detector.detect(text)
        col = 0
        while len(spells) > 0 and col < 1:
            # spell_ind = 0
            spell_ind = random.randint(0, len(spells) - 1)
            new_text = spells[spell_ind].word + ' <sep> ' + text[: spells[spell_ind].interval[0]] + '<mask>'+ text[spells[spell_ind].interval[1]: ]
            print(f'Task: |{new_text}|')
            ans_ids = self.model.generate(self.tokenizer([new_text], return_tensors='pt').to(self.device)["input_ids"],
                                      num_beams=5, min_length=5, max_length=500)
            ans_tokens = self.tokenizer.batch_decode(ans_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
            corr_word = ' '.join(ans_tokens)
            print( f'Corr word: |{corr_word}|')
            text = text[: spells[spell_ind].interval[0]] + corr_word + text[spells[spell_ind].interval[1]: ]
            spells = HunspellDetector().detect(text)
            col += 1

        return text


def spellcheck_model_test(model: SpellCheckModelBase):
    text_noise = 'Moroever I have chosen this month because I think the weather will be fine.'
    text_gt = 'Moreover I have chosen this month because I think the weather will be fine.'
    text_result = model.correct(text_noise)
    print(f'\nSpellcheck model testing\n\nModel: {str(model)}\n\n{text_noise} - Noised text\n{text_gt} - GT text'
          f'\n{text_result} - Result text')


def char_based_model_check():
    # checkpoint = PATH_PREFIX + 'training/checkpoints/model_big_0_9.pt'
    checkpoint = 'No learning'
    d_model = 256
    model = CharBasedTransformerChecker(config={'d_model': d_model, 'encoder_layers': 6, 'decoder_layers': 6,
                                         'encoder_attention_heads': 8, 'decoder_attention_heads': 8,
                                         'encoder_ffn_dim': d_model * 4, 'decoder_ffn_dim': d_model * 4},
                                 checkpoint=checkpoint)
    spellcheck_model_test(model)


def bart_pretrain_test():
    # model = BartChecker(model=BartForConditionalGeneration.from_pretrained('facebook/bart-base'))
    checker = BartChecker()
    checker.from_pretrained()

    """ spell correction """
    print(checker.correct("I luk foward to receving your reply"))
    # → "I look forward to receiving your reply"
    print(checker.correct_strings(["I luk foward to receving your reply", ]))
    # → ["I look forward to receiving your reply"]

    """ evaluation of models """
    # texts_gt, texts_noise = get_texts_from_file(PATH_PREFIX + 'dataset/bea/bea2.gt'), \
    #                         get_texts_from_file(PATH_PREFIX + 'dataset/bea/bea2.noise')
    # evaluate(checker, texts_gt=texts_gt, texts_noise=texts_noise)
    # spellcheck_model_test(checker)


def bert_sep_mask_all_test():
    model = BartSepMaskAllChecker()
    model.from_pretrained()
    spellcheck_model_test(model)


def bart_mask_word_test():
    # model = MaskWordBART(model=BartForConditionalGeneration.from_pretrained('facebook/bart-base'))
    for i in range(4, 8):
        try:
            model = MaskWordBartChecker(checkpoint=PATH_PREFIX + 'training/checkpoints/bart-mask-word_v1_3.pt',
                                 device=torch.device(f'cuda:{i}'))
            spellcheck_model_test(model)
            return
        except Exception as e:
            print(e)
            raise e
            # continue
    print('All GPUs bad')


def detector_candidator_ranker_test():
    model = DetectorCandidatorRanker()
    spellcheck_model_test(model)


if __name__ == '__main__':
    print(HunspellDetector().is_spelled('allowed'))
