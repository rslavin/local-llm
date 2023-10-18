#!/usr/bin/env python3
import os
import sys
import time
import tqdm
import json
import torch
import logging
import requests
import contextlib
import numpy as np

from PIL import Image
from io import BytesIO

from termcolor import cprint, colored
from tabulate import tabulate

from huggingface_hub import snapshot_download, hf_hub_download, login


ImageExtensions = ('.png', '.jpg', '.jpeg', '.tiff', '.bmp', '.gif')


def load_image(path):
    """
    Load an image from a local path or URL
    """
    if path.startswith('http') or path.startswith('https'):
        logging.debug(f'-- downloading {path}')
        response = requests.get(path)
        image = Image.open(BytesIO(response.content)).convert('RGB')
    else:
        logging.debug(f'-- loading {path}')
        image = Image.open(path).convert('RGB')
        
    return image
    
    
def load_prompts(prompts):
    """
    Load prompts from a list of txt or json files
    (or if these are strings, just return the strings)
    """
    if isinstance(prompts, str):
        prompts = [prompts]
        
    prompt_list = []
    
    for prompt in prompts:
        ext = os.path.splitext(prompt)[1]
        
        if ext == '.json':
            with open(prompt) as file:
                json_prompts = json.load(file)
            for json_prompt in json_prompts:
                if isinstance(json_prompt, dict):
                    prompt_list.append(json_prompt['text'])
                elif isinstance(json_prompt, str):
                    prompt_list.append(json_prompt)
                else:
                    raise TypeError(f"{type(json_prompt)}")
        elif ext == '.txt':
            with open(prompt) as file:
                prompt_list.append(file.read())
        else:
            prompt_list.append(prompt)
            
    return prompt_list
    

def download_model(model, type='model', cache_dir='$TRANSFORMERS_CACHE'):
    """
    Get the local path to a cached model or file in the cache_dir, or download it from HuggingFace Hub if needed.
    If the asset is private and authentication is required, set the HUGGINGFACE_TOKEN environment variable.
    cache_dir is where the model gets downloaded to - by default, set to $TRANSFORMERS_CACHE (/data/models/huggingface)
    """
    token = os.environ.get('HUGGINGFACE_TOKEN', os.environ.get('HUGGING_FACE_HUB_TOKEN'))
    
    if token:
        login(token=token)
       
    if not cache_dir or cache_dir == '$TRANSFORMERS_CACHE':
        cache_dir = os.environ.get('TRANSFORMERS_CACHE', '/root/.cache/huggingface')
        
    # handle either "org/repo" or individual "org/repo/file"
    # the former has 0-1 slashes, while the later has 2.
    num_slashes = 0
    
    for c in model:
        if c == '/':
            num_slashes += 1
            
    if num_slashes >= 2:  
        slash_count = 0
        
        for idx, i in enumerate(model):
            if i == '/':
                slash_count += 1
                if slash_count == 2:
                    break
                    
        repo_id = model[:idx]
        filename = model[idx+1:]
        
        repo_path = hf_hub_download(repo_id=repo_id, filename=filename, repo_type=type, cache_dir=cache_dir, resume_download=True)
    else:
        repo_path = snapshot_download(repo_id=model, repo_type=type, cache_dir=cache_dir, resume_download=True)
        
    return repo_path
    
    
def default_model_api(model_path, quant_path=None):
    """
    Given the local path to a model, determine the type of API to use to load it.
    TODO check the actual model files / configs instead of just parsing the paths
    """
    if quant_path:
        quant_api = default_model_api(quant_path)
        
        if quant_api != 'hf':
            return quant_api

    model_path = model_path.lower()

    if 'ggml' in model_path or 'ggml' in model_path:
        return 'llama.cpp'
    elif 'gptq' in model_path:
        return 'auto_gptq'  # 'exllama'
    elif 'awq' in model_path:
        return 'awq'
    elif 'mlc' in model_path:
        return 'mlc'
    else:
        return 'hf'
        
        
def print_table(rows, header=None, footer=None, color='green', attrs=None):
    """
    Print a table from a list[list] of rows/columns, or a 2-column dict 
    where the keys are column 1, and the values are column 2.
    
    Header is a list of columns or rows that are inserted at the top.
    Footer is a list of columns or rows that are added to the end.
    
    color names and style attributes are from termcolor library:
      https://github.com/termcolor/termcolor#text-properties
    """
    if isinstance(rows, dict):
        rows = [[key,value] for key, value in rows.items()]    

    if header:
        if not isinstance(header[0], list):
            header = [header]
        rows = header + rows
        
    if footer:
        if not isinstance(footer[0], list):
            footer = [footer]
        rows = rows + footer
        
    cprint(tabulate(rows, tablefmt='simple_grid', numalign='center'), color, attrs=attrs)


def replace_text(text, dict):
    """
    Replace instances of each of the keys in dict in the text string with the values in dict
    """
    for key, value in dict.items():
        text = text.replace(key, value)
    return text    
    
    
class AttrDict(dict):
    """
    A dict where keys are available as attributes
    https://stackoverflow.com/a/14620633
    """
    def __init__(self, *args, **kwargs):
        super(AttrDict, self).__init__(*args, **kwargs)
        self.__dict__ = self
                
  
class cudaArrayInterface():
    """
    Exposes __cuda_array_interface__ - typically used as a temporary view into a larger buffer
    https://numba.readthedocs.io/en/stable/cuda/cuda_array_interface.html
    """
    def __init__(self, data, shape, dtype=np.float32):
        if dtype == np.float32:
            typestr = 'f4'
        elif dtype == np.float64:
            typestr = 'f8'
        elif dtype == np.float16:
            typestr = 'f2'
        else:
            raise RuntimeError(f"unsupported dtype:  {dtype}")
            
        self.__cuda_array_interface__ = {
            'data': (data, False),  # R/W
            'shape': shape,
            'typestr': typestr,
            'version': 3,
        }  
        

torch_dtype_dict = {
    'bool'       : torch.bool,
    'uint8'      : torch.uint8,
    'int8'       : torch.int8,
    'int16'      : torch.int16,
    'int32'      : torch.int32,
    'int64'      : torch.int64,
    'float16'    : torch.float16,
    'float32'    : torch.float32,
    'float64'    : torch.float64,
    'complex64'  : torch.complex64,
    'complex128' : torch.complex128
}

def torch_dtype(dtype):
    """
    Convert numpy.dtype or str to torch.dtype
    """
    return torch_dtype_dict[str(dtype)]
    
    
# https://stackoverflow.com/a/37243211    
class TQDMRedirectStdOut(object):
  file = None
  def __init__(self, file):
    self.file = file

  def write(self, x):
    if len(x.rstrip()) > 0:  # Avoid print() second call (useless \n)
        tqdm.tqdm.write(x, file=self.file)

@contextlib.contextmanager
def tqdm_redirect_stdout():
    save_stdout = sys.stdout
    sys.stdout = TQDMRedirectStdOut(sys.stdout)
    yield
    sys.stdout = save_stdout
    
    
# add custom logging.SUCCESS level and logging.success() function
logging.SUCCESS = 35 # https://docs.python.org/3/library/logging.html#logging-levels

class LogFormatter(logging.Formatter):
    """
    Colorized log formatter (inspired from https://stackoverflow.com/a/56944256)
    Use LogFormatter.config() to enable it with the desired logging level.
    """
    DefaultFormat = "%(asctime)s | %(levelname)s | %(message)s"
    DefaultDateFormat = "%H:%M:%S"
    
    DefaultColors = {
        logging.DEBUG: ('light_grey', 'dark'),
        logging.INFO: None,
        logging.WARNING: 'yellow',
        logging.SUCCESS: 'green',
        logging.ERROR: 'red',
        logging.CRITICAL: 'red'
    }

    @staticmethod
    def config(level='info', format=DefaultFormat, datefmt=DefaultDateFormat, colors=DefaultColors, **kwargs):
        """
        Configure the root logger with formatting and color settings.
        
        Parameters:
          level (str|int) -- Either the log level name 
          format (str) -- Message formatting attributes (https://docs.python.org/3/library/logging.html#logrecord-attributes)
          
          datefmt (str) -- Date/time formatting string (https://docs.python.org/3/library/logging.html#logging.Formatter.formatTime)
          
          colors (dict) -- A dict with keys for each logging level that specify the color name to use for those messages
                           You can also specify a tuple for each couple, where the first entry is the color name,
                           followed by style attributes (from https://github.com/termcolor/termcolor#text-properties)
                           If colors is None, then colorization will be disabled in the log.
                           
          kwargs (dict) -- Additional arguments passed to logging.basicConfig() (https://docs.python.org/3/library/logging.html#logging.basicConfig)
        """
        logging.addLevelName(logging.SUCCESS, "SUCCESS")

        def log_success(*args, **kwargs):
            logging.log(logging.SUCCESS, *args, **kwargs)
            
        logging.success = log_success

        if isinstance(level, str):
            level = getattr(logging, level.upper(), logging.INFO)

        log_handler = logging.StreamHandler()
        log_handler.setFormatter(LogFormatter())
        #log_handler.setLevel(level)
        
        logging.basicConfig(handlers=[log_handler], level=level, **kwargs)
    
    def __init__(self, format=DefaultFormat, datefmt=DefaultDateFormat, colors=DefaultColors):
        """
        @internal it's recommended to use LogFormatter.config() above
        """
        self.formatters = {}
        
        for level in self.DefaultColors:
            if colors is not None and level in colors and colors[level] is not None:
                color = colors[level]
                attrs = None
                
                if not isinstance(color, str):
                    attrs = color[1:]
                    color = color[0]

                fmt = colored(format, color, attrs=attrs)
            else:
                fmt = format
                
            self.formatters[level] = logging.Formatter(fmt=fmt, datefmt=datefmt)

    def format(self, record):
        """
        Implementation of logging.Formatter record formatting function
        """
        return self.formatters[record.levelno].format(record)
