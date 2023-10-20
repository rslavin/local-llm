#!/usr/bin/env python3
import os
import sys
import time
import signal
import logging
import numpy as np

from termcolor import cprint

from local_llm import LocalLM, ChatHistory, ChatTemplates
from local_llm.utils import ImageExtensions, ArgParser, load_prompts, print_table 

# see utils/args.py for options
parser = ArgParser()
parser.add_argument("--no-streaming", action="store_true", help="wait to output entire reply instead of token by token")
args = parser.parse_args()

prompts = load_prompts(args.prompt)

# load model
model = LocalLM.from_pretrained(
    args.model, 
    quant=args.quant, 
    api=args.api,
    vision_model=args.vision_model
)

# create the chat history
chat_history = ChatHistory(model, args.chat_template, args.system_prompt)

# make an interrupt handler for muting the bot output
last_interrupt = 0.0
interrupt_chat = False

def on_interrupt(signum, frame):
    """
    Ctrl+C handler - if done once, interrupts the LLM
    If done twice in succession, exits the program
    """
    global last_interrupt
    global interrupt_chat
    
    curr_time = time.perf_counter()
    time_diff = curr_time - last_interrupt
    last_interrupt = curr_time
    
    if time_diff > 2.0:
        logging.warning("Ctrl+C:  interrupting chatbot")
        interrupt_chat = True
    else:
        while True:
            logging.warning("Ctrl+C:  exiting...")
            sys.exit(0)
            time.sleep(0.5)
               
signal.signal(signal.SIGINT, on_interrupt)


while True: 
    # get the next prompt from the list, or from the user interactivey
    if isinstance(prompts, list):
        if len(prompts) > 0:
            user_prompt = prompts.pop(0)
            cprint(f'>> PROMPT: {user_prompt}', 'blue')
        else:
            break
    else:
        cprint('>> PROMPT: ', 'blue', end='', flush=True)
        user_prompt = sys.stdin.readline().strip()
    
    print('')
    
    # special commands:  load prompts from file
    # 'reset' or 'clear' resets the chat history
    if user_prompt.lower().endswith(('.txt', '.json')):
        user_prompt = ' '.join(load_prompts(user_prompt))
    elif user_prompt.lower() == 'reset' or user_prompt.lower() == 'clear':
        logging.info("resetting chat history")
        chat_history.reset()
        continue

    # add the latest user prompt to the chat history
    entry = chat_history.append(role='user', msg=user_prompt)

    # images should be followed by text prompts
    if 'image' in entry and 'text' not in entry:
        logging.debug("image message, waiting for user prompt")
        continue
        
    # get the latest embeddings from the chat
    embedding, position = chat_history.embed_chat()
    
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        logging.debug(f"adding embedding shape={embedding.shape} position={position}")

    # generate bot reply
    output = model.generate(
        embedding, 
        streaming=not args.no_streaming, 
        kv_cache=chat_history.kv_cache,
        max_new_tokens=args.max_new_tokens,
        min_new_tokens=args.min_new_tokens,
        do_sample=args.do_sample,
        repetition_penalty=args.repetition_penalty,
        temperature=args.temperature,
        top_p=args.top_p,
    )
        
    bot_reply = chat_history.append(role='bot', text='') # placeholder
    
    if args.no_streaming:
        bot_reply.text = output
        cprint(output, 'green')
    else:
        for token in output:
            bot_reply.text += token
            cprint(token, 'green', end='', flush=True)
            if interrupt_chat:
                output.stop()
                interrupt_chat = False
                break
            
    print('\n')
    print_table(model.stats)
    print('')
    
    chat_history.kv_cache = output.kv_cache   # save the kv_cache 
    bot_reply.text = output.output_text  # sync the text once more
 
#logging.warning('exiting...')
