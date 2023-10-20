#!/usr/bin/env python3
import os
import re
import time
import json
import torch
import logging

from transformers import AutoConfig

from .vision import CLIPImageEmbedding, MMProjector
from .utils import AttributeDict, download_model, default_model_api, print_table


class LocalLM():
    """
    Base class for local LLM APIs. It defines common Huggingface-like interfaces for
    model loading, text generation, chat, tokenization/detokenization, and streaming.
    It also supports vision models like Llava and generating image embeddings with CLIP.
    
    Supported API backends include: AutoGPTQ, AWQ, MLC (TODO llama.cpp, exllama2)
    
    Use LocalLM.from_pretrained() rather than instantiating this class directly.
    """
    @staticmethod
    def from_pretrained(model, api=None, **kwargs):
        """
        Load a model from the given path or download it from HuggingFace Hub.
        If the API isn't specified, it will be inferred from the type of model.
        
        Parameters:
        
          model (str) -- either the path to the model, or HuggingFace model repo/name
          api (str) -- the model backend API to use:  'auto_gptq', 'awq', 'mlc', or 'hf'
                       if left as None, it will attempt to be automatically determined.
                       
        kwargs:
        
          quant (str) -- for AWQ or MLC, either specify the quantization method,
                         or the path to the quantized model (AWQ and MLC API's only)

          vision_model (str) -- for VLMs, override the vision embedding model (CLIP)
                                otherwise, it will use the CLIP variant from the config.
        """
        if os.path.isdir(model) or os.path.isfile(model):
            model_path = model
            model_name = os.path.basename(model_path)
        else:
            model_path = download_model(model)
            model_name = model
            
        if not api:
            api = default_model_api(model_path, quant)
            
        logging.info(f"loading {model_path} with {api.upper()}")
        load_begin = time.perf_counter()
        
        # doing this imports here avoid circular import, and makes it so these
        # dependencies are only needed if they are actually used to load a model
        if api == 'auto_gptq':
            from local_llm.models import AutoGPTQModel
            model = AutoGPTQModel(model_path, **kwargs)
        elif api == 'awq':
            from local_llm.models import AWQModel
            model = AWQModel(model_path, **kwargs)
        elif api == 'mlc':
            from local_llm.models import MLCModel
            model = MLCModel(model_path, **kwargs)
        elif api == 'hf':
            from local_llm.models import HFModel
            model = HFModel(model_path, **kwargs)
        else:
            raise ValueError(f"invalid API: {api}")
        
        if 'name' not in model.config or not model.config.name:
            model.config.name = model_name
            
        model.config.api = api
        model.config.load_time = time.perf_counter() - load_begin
        
        print_table(model.config)
        return model
     
    def generate(self, inputs, streaming=True, **kwargs):
        """
        Generate output from input text or an embedding.
        
        Parameters:
          inputs (str|list[int]|torch.Tensor|np.ndarray) -- the prompt string or embedding
          streaming (bool) -- if true (default), an iterator will be returned that outputs
                              one token at a time.  Otherwise, return the full response.
                              
        kwargs -- see https://huggingface.co/docs/transformers/main/en/main_classes/text_generation  
        
          max_new_tokens (int) -- the number of tokens to output in addition to the prompt (default: 128)
          min_new_tokens (int) -- force the model to generate a set number of output tokens (default: -1)
          do_sample (bool) -- if True, temperature/top_p will be used.  Otherwise, greedy search (default: False)
          repetition_penalty -- the parameter for repetition penalty. 1.0 means no penalty (default: 1.0)  
          temperature (float) -- randomness token sampling parameter (default=0.7, only used if do_sample=True)
          top_p (float) -- if set to float < 1 and do_sample=True, only the smallest set of most probable tokens
                           with probabilities that add up to top_p or higher are kept for generation (default 0.95)
          stop_tokens (list[int]) -- defaults to EOS token ID
          kv_cache (ndarray) -- previous kv_cache that the inputs will be appended to.  By default, a blank kv_cache 
                                will be created for each generation (i.e. a new chat).  This generation's kv_cache
                                will be set in the returned StreamingResponse iterator after the request is complete.

        Returns:
          If streaming is true, a StreamingResponse iterator is returned that outputs one decoded token string at a time.
          Otherwise, this function blocks and a string containing the full reply is returned after it's been completed.
        """
        raise NotImplementedError("use LLM.from_pretrained() as opposed to instantiating an LLM object directly")

    def embed_text(self, text, **kwargs):
        raise NotImplementedError("embed_text() not implemented for this model")
        
    def embed_tokens(self, tokens, **kwargs):
        raise NotImplementedError("embed_tokens() not implemented for this model")
       
    def embed_image(self, image, crop=True, return_tensors='pt', **kwargs):
        assert(self.has_vision)
        
        embedding = self.vision(image, crop=crop, hidden_state=self.model_config.mm_vision_select_layer)
        embedding = self.mm_projector(embedding[:, 1:])

        logging.debug(f"image_embedding  shape={embedding.shape}  dtype={embedding.dtype}  device={embedding.device}")
        
        if return_tensors == 'pt':
            return embedding
        elif return_tensors == 'np':
            return embedding.detach().cpu().numpy()
        else:
            raise ValueError(f"return_tensors should be 'np' or 'pt' (was '{return_tensors}')")
        
    def __init__(self, model_path, **kwargs):
        """
        @internal this is down here because it should only be used by inherited classes.
        """
        self.config = AttributeDict()
        self.stats = AttributeDict()
        
        self.config.name = ''
        self.config.api = ''
        
        self.model_path = model_path
        self.model_config = AutoConfig.from_pretrained(model_path)
        
        self.init_vision(**kwargs)
        
    def init_vision(self, **kwargs):
        """
        Init vision embedding/projection models for VLMs like llava, MiniGPT-4, ect.
        @internal this function is automatically called by LocalLM initializer.
        """
        self.has_vision = 'llava' in self.model_config._name_or_path.lower()
        
        for arch in self.model_config.architectures:
            if 'llava' in arch.lower():
                self.has_vision = True

        if not self.has_vision:
            return
           
        # load the image embedding model
        self.vision = CLIPImageEmbedding.from_pretrained(
            kwargs.get('vision_model') if kwargs.get('vision_model')
            else self.model_config.mm_vision_tower,
            dtype=torch.float16,
        ) 
        
        # create image embedding projection model
        self.mm_projector = MMProjector.from_pretrained(
            self.model_path, self.vision.dtype
        )
        
