import re
import numpy as np
from transformers import MarianMTModel, MarianTokenizer
from onnxruntime import InferenceSession


class TranslatorTorch():
    def __init__(self, name, split_regex):
        self.split_regex = split_regex
        self.tokenizer = MarianTokenizer.from_pretrained(name)
        self.model = MarianMTModel.from_pretrained(name)
    
    def _prepare_text(self, text):
        text_filtered = text.replace('\n', ' ').strip()
        return re.split(self.split_regex, text_filtered)

    def translate(self, text):
        sentences = self._prepare_text(text)
        batches = self.tokenizer.prepare_seq2seq_batch(sentences, return_tensors="pt")
        batches_translated = self.model.generate(**batches)
        sentences_translated = [self.tokenizer.decode(s, skip_special_tokens=True) for s in batches_translated]
        return " ".join(sentences_translated)
    
    def save_model(self, name):
        self.tokenizer.save_pretrained(name)
        self.model.save_pretrained(name)


class TranslationEncoderOnnx:
    def __init__(self):
        super().__init__()
        self.encoder_session = InferenceSession("onnx/encoder.onnx")

    def __call__(self, input_ids, attention_mask):
        onnx_inputs = {
            'input_ids': input_ids,
            'attention_mask': attention_mask
        }
        hidden = self.encoder_session.run(None, onnx_inputs)
        return hidden[0]


class TranslationDecoderOnnx:
    def __init__(self, max_length):
        super().__init__()
        self.decoder_session = InferenceSession("onnx/decoder.onnx")
        self.lm_head_session = InferenceSession("onnx/lm_head.onnx")

        self.max_length = max_length

    def __call__(self, input_ids, encoder_outputs, encoder_attention_mask, index):
        attention_mask = np.ones_like(input_ids)
        attention_mask[:, index + 1:] = 0

        decoder_onnx_inputs = {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'encoder_hidden_states': encoder_outputs,
            'encoder_attention_mask': encoder_attention_mask
        }
        hidden = self.decoder_session.run(None, decoder_onnx_inputs)[0]

        _, n_length, _ = hidden.shape
        mask_selection = np.arange(n_length) == index
        mask_selection = mask_selection.reshape(1, -1, 1)
        masked_selection = np.multiply(hidden, mask_selection)
        summed = np.sum(masked_selection, 1)
        hidden_masked = np.expand_dims(summed, 1)

        lm_head_onnx_inputs = {'input': hidden_masked}
        output = self.lm_head_session.run(None, lm_head_onnx_inputs)
        return output[0]


class TranslationModelOnnx:
    def __init__(self, max_length, config):
        self.encoder = TranslationEncoderOnnx()
        self.decoder = TranslationDecoderOnnx(max_length)

        self.max_length = max_length
        self.config = config

    def generate(self, tokens):
        enc_inputs, enc_att_mask = tokens['input_ids'].numpy(), tokens['attention_mask'].numpy()
        hidden = self.encoder(enc_inputs, enc_att_mask)

        indices_active = np.arange(enc_inputs.shape[0])
        dec_inputs = np.ones_like(enc_inputs)*self.config.pad_token_id
        dec_inputs[:, 0] = self.config.decoder_start_token_id
        dec_outputs = np.copy(dec_inputs)

        for idx in range(self.max_length - 1):
            logits = self.decoder(
                dec_inputs[indices_active, :],
                hidden[indices_active, :],
                enc_att_mask[indices_active, :],
                idx
            )
            logits[:, 0, self.config.pad_token_id] = float("-inf")
            token_ids = logits.argmax(axis=2).flatten()

            dec_inputs[indices_active, idx + 1] = token_ids

            indices_end = np.where(dec_inputs[:, idx + 1] == self.config.eos_token_id)[0]
            indices_active = indices_active[np.logical_not(np.isin(indices_active, indices_end))]
            dec_outputs[indices_end, :] = dec_inputs[indices_end, :]

            if indices_active.size == 0:
                break
        return dec_inputs


class TranslatorOnnx():
    def __init__(self, name, split_regex, max_length):
        self.split_regex = split_regex
        self.max_length = max_length
        self.tokenizer = MarianTokenizer.from_pretrained(name)

        config_file = MarianMTModel.from_pretrained(name).config
        self.model = TranslationModelOnnx(max_length, config_file)
    
    def _prepare_text(self, text):
        text_filtered = text.replace('\n', ' ').strip()
        return re.split(self.split_regex, text_filtered)

    def translate(self, text):
        sentences = self._prepare_text(text)
        batches = self.tokenizer.prepare_seq2seq_batch(
            sentences,
            return_tensors="pt",
            max_length=self.max_length,
            padding='max_length'
        )
        batches_translated = self.model.generate(batches)
        sentences_translated = [self.tokenizer.decode(s, skip_special_tokens=True) for s in batches_translated]
        return " ".join(sentences_translated)
