
from transformers import RobertaPreTrainedModel, RobertaModel, RobertaTokenizer
from transformers.models.roberta.modeling_roberta import RobertaLayer
from transformers.modeling_outputs import SequenceClassifierOutput
import math
from typing import List, Optional, Tuple, Union

import torch
import torch.utils.checkpoint
from torch import nn
from torch.nn import BCEWithLogitsLoss, CrossEntropyLoss, MSELoss
import torch.nn.functional as F
import numpy as np


class RobertaClassificationHead(nn.Module):
    """Head for sentence-level classification tasks."""

    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        classifier_dropout = (
            config.classifier_dropout if config.classifier_dropout is not None else config.hidden_dropout_prob
        )
        self.dropout = nn.Dropout(classifier_dropout)
        self.out_proj = nn.Linear(config.hidden_size, config.num_labels)

    def forward(self, features, **kwargs):
        x = features[:, 0, :]  # take <s> token (equiv. to [CLS])
        x = self.dropout(x)
        x = self.dense(x)
        x = torch.tanh(x)
        x = self.dropout(x)
        x = self.out_proj(x)
        return x


def split_ques_context(sequence_output, pq_end_pos, ques_max_len, seq_max_len):
    ques_max_len = ques_max_len
    context_max_len = seq_max_len
    sep_tok_len = 2
    ques_sequence_output = sequence_output.new(
        torch.Size((sequence_output.size(0), ques_max_len, sequence_output.size(2)))).zero_()
    context_sequence_output = sequence_output.new_zeros(
        (sequence_output.size(0), context_max_len, sequence_output.size(2)))
    context_attention_mask = sequence_output.new_zeros((sequence_output.size(0), context_max_len))
    ques_attention_mask = sequence_output.new_zeros((sequence_output.size(0), ques_max_len))
    for i in range(0, sequence_output.size(0)):
        q_end = pq_end_pos[i][0]
        p_end = pq_end_pos[i][1]
        ques_sequence_output[i, :min(ques_max_len, q_end)] = sequence_output[i,
                                                                   1: 1 + min(ques_max_len, q_end)]
        context_sequence_output[i, :min(context_max_len, p_end - q_end - sep_tok_len)] = sequence_output[i,
                                                                                     q_end + sep_tok_len + 1: q_end + sep_tok_len + 1 + min(
                                                                                         p_end - q_end - sep_tok_len,
                                                                                         context_max_len)]
        context_attention_mask[i, :min(context_max_len, p_end - q_end - sep_tok_len)] = sequence_output.new_ones(
            (1, context_max_len))[0, :min(context_max_len, p_end - q_end - sep_tok_len)]
        ques_attention_mask[i, : min(ques_max_len, q_end)] = sequence_output.new_ones((1, ques_max_len))[0,
                                                                   : min(ques_max_len, q_end)]
    return ques_sequence_output, context_sequence_output, ques_attention_mask, context_attention_mask


def masked_softmax(vector: torch.Tensor,
                   mask: torch.Tensor,
                   dim: int = -1,
                   memory_efficient: bool = False,
                   mask_fill_value: float = -1e32) -> torch.Tensor:
    if mask is None:
        result = torch.nn.functional.softmax(vector, dim=dim)
    else:
        mask = mask.float()
        #mask = mask.half()

        while mask.dim() < vector.dim():
            mask = mask.unsqueeze(1)
        if not memory_efficient:
            # To limit numerical errors from large vector elements outside the mask, we zero these out.
            result = torch.nn.functional.softmax(vector * mask, dim=dim)
            result = result * mask
            result = result / (result.sum(dim=dim, keepdim=True) + 1e-13)
        else:
            masked_vector = vector.masked_fill((1 - mask).byte(), mask_fill_value)
            result = torch.nn.functional.softmax(masked_vector, dim=dim)
    return result

class SCAttention(nn.Module) :
    def __init__(self, input_size, hidden_size) :
        super(SCAttention, self).__init__()
        self.hidden_size = hidden_size
        self.W = nn.Linear(input_size, hidden_size)
        self.map_linear = nn.Linear(hidden_size, hidden_size)
        self.init_weights()

    def init_weights(self) :
        nn.init.xavier_uniform_(self.W.weight.data)
        self.W.bias.data.fill_(0.1)

    def forward(self, passage, question, q_mask):
        Wp = passage
        Wq = question
        scores = torch.bmm(Wp, Wq.transpose(2, 1))
        mask = q_mask.unsqueeze(1).repeat(1, passage.size(1), 1)
        # scores.data.masked_fill_(mask.data, -float('inf'))
        alpha = masked_softmax(scores, mask)
        output = torch.bmm(alpha, Wq)
        output = nn.ReLU()(self.map_linear(output))
        #output = self.map_linear(all_con)
        return output

class RobertaForAspectAttention(RobertaPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.num_labels = config.num_labels
        self.roberta = RobertaModel(config, add_pooling_layer=False)
        self.attention = SCAttention(config.hidden_size, config.hidden_size)
        self.classifier = RobertaClassificationHead(config)
        self.loss_weight = torch.tensor([0.21247951408810348, 1.653269016173937]).cuda()
        self.post_init()
        
    def forward(self, input_ids=None, attention_mask=None, token_type_ids=None, pq_end_pos=None, 
        position_ids: Optional[torch.LongTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None):

        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.roberta(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        
        sequence_output = outputs[0]

        query_sequence_output, context_sequence_output, query_attention_mask, context_attention_mask = \
            split_ques_context(sequence_output, pq_end_pos, 10, sequence_output.size(1))

        if self.config.sc_aspect:
            sequence_output = self.attention(sequence_output, query_sequence_output, query_attention_mask)
        else:
            sequence_output = self.attention(sequence_output, context_sequence_output, context_attention_mask)

        sequence_output = sequence_output + outputs[0]

        logits = self.classifier(sequence_output)

        loss = None
        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"

            if self.config.problem_type == "regression":
                loss_fct = MSELoss()
                if self.num_labels == 1:
                    loss = loss_fct(logits.squeeze(), labels.squeeze())
                else:
                    loss = loss_fct(logits, labels)
            elif self.config.problem_type == "single_label_classification":
                loss_fct = CrossEntropyLoss(weight=self.loss_weight)
                loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
            elif self.config.problem_type == "multi_label_classification":
                loss_fct = BCEWithLogitsLoss()
                loss = loss_fct(logits, labels)

        if not return_dict:
            output = (logits,) + outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return SequenceClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


class RobertaForSequenceClassification(RobertaPreTrainedModel):
    _keys_to_ignore_on_load_missing = [r"position_ids"]

    def __init__(self, config):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.config = config

        self.roberta = RobertaModel(config, add_pooling_layer=False)
        self.classifier = RobertaClassificationHead(config)

        self.loss_weight = torch.tensor([0.21247951408810348, 1.653269016173937]).cuda()

        # Initialize weights and apply final processing
        self.post_init()

    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.FloatTensor] = None,
        token_type_ids: Optional[torch.LongTensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        head_mask: Optional[torch.FloatTensor] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        labels: Optional[torch.LongTensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple[torch.Tensor], SequenceClassifierOutput]:
        r"""
        labels (`torch.LongTensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the sequence classification/regression loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels == 1` a regression loss is computed (Mean-Square loss), If
            `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.roberta(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        sequence_output = outputs[0]
        logits = self.classifier(sequence_output)

        loss = None
        if labels is not None:
            if self.config.problem_type is None:
                if self.num_labels == 1:
                    self.config.problem_type = "regression"
                elif self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
                    self.config.problem_type = "single_label_classification"
                else:
                    self.config.problem_type = "multi_label_classification"

            loss_fct = CrossEntropyLoss(weight = self.loss_weight)
            loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))


        if not return_dict:
            output = (logits,) + outputs[2:]
            return ((loss,) + output) if loss is not None else output

        return SequenceClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )

if __name__ == "__main__":
    tokenizer = RobertaTokenizer.from_pretrained(
        "/media/Z/namlh31/vi-roberta-base/"
    )
    result = tokenizer("Hệ thống nhà hàng phục vụ khách du lịch", "Anh yeu em lam luon", padding="max_length", max_length=20, truncation="only_second")
    p_end_pos = sum(result['attention_mask'])-2
    q_end_pos = result['input_ids'].index(tokenizer.sep_token_id) -1
    result['pq_end_pos'] = [q_end_pos, p_end_pos]

    print(result)