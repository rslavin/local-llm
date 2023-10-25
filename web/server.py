#!/usr/bin/env python3
import os
import ssl
import flask
import queue
import pprint
import logging
import threading

from local_llm.utils import ArgParser
from .voice_chat import VoiceChat

from websockets.sync.server import serve as websocket_serve

class WebServer
    """
    HTTP/HTTPS Flask webserver with websocket messaging.
    
    Use this by either creating an instance and providing msg_callback,
    or inherit from it and implement on_message() in a subclass.
    
    You can also add Flask routes to Webserver.app before start() is called.
    """
    def __init__(self, web_host='0.0.0.0', 
                 web_port=8050, ws_port=49000,
                 ssl_cert=None, ssl_key=None, 
                 index='index.html', msg_callback=None,
                 **kwargs):
        """
        Parameters:
        
          web_host (str) -- network interface to bind to (0.0.0.0 for all)
          web_port (int) -- port to serve HTTP/HTTPS webpages on
          ws_port (int) -- port to use for websocket communication
          ssl_cert (str) -- path to PEM-encoded SSL/TLS cert file for enabling HTTPS
          ssl_key (str) -- path to PEM-encoded SSL/TLS cert key for enabling HTTPS
        """
        super().__init__(**kwargs)

        self.host = web_host
        self.port = web_port
        self.root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../www'))
        
        logging.debug(f"webserver root directory: {self.root}")
        
        # flask server
        self.app = flask.Flask(__name__, 
            static_folder=os.path.join(self.root, 'static'),
            template_folder=os.path.join(self.root, 'templates')
        )
        
        self.app.add_url_rule('/', view_func=self.on_index, methods=['GET'])
        
        self.index = index
        self.message_callback = msg_callback
        
        # SSL / HTTPS
        self.ssl_key = ssl_key
        self.ssl_cert = ssl_cert
        self.ssl_context = None
        self.web_protocol = "http"
        
        if self.ssl_cert and self.ssl_key:
            self.web_protocol = "https"
            self.ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            self.ssl_context.load_cert_chain(certfile=self.ssl_cert, keyfile=self.ssl_key)
            
        # websocket
        self.ws_port = ws_port
        self.ws_queue = queue.Queue()
        
        self.ws_server = websocket_serve(self.on_websocket, host=self.host, port=self.ws_port, ssl_context=self.ssl_context)
        self.ws_thread = threading.Thread(target=lambda: self.ws_server.serve_forever(), daemon=True)
        self.web_thread = threading.Thread(target=lambda: self.app.run(host=self.host, port=self.port, ssl_context=self.ssl_context, debug=True, use_reloader=False), daemon=True)
          
    def start(self):
        """
        Call this to start the webserver threads
        """
        logging.info(f"starting webserver @ {self.web_protocol}://{self.host}:{self.port}")
        self.ws_thread.start()
        self.web_thread.start()

    def on_message(self, payload, msg_type, timestamp):
        """
        Implement this function in a subclass to process messages
        Otherwise, msg_callback needs to be provided during initialization
        """
        if self.msg_callback is not None:
           self.msg_callback(payload, msg_type, timestamp)
        else:
            raise NotImplementedError(f"{type(self)} did not implement on_message or have a msg_callback provided")
            
        '''
        if type == 0:  # JSON
            if 'chat_history_reset' in msg:
                self.llm.chat_history.reset()
                self.send_chat_history(self.llm.chat_history)
            if 'client_state' in msg:
                if msg['client_state'] == 'connected':
                    threading.Timer(1.0, lambda: self.send_chat_history(self.llm.chat_history)).start()
            if 'tts_voice' in msg:
                self.tts.voice = msg['tts_voice']
        elif type == 1:  # text (chat input)
            self.prompt(msg)
        elif type == 2:  # web audio (mic)
            self.asr(msg)
        '''
     
    def send_message(self, payload, type=None, timestamp=None):
        """
        Send a websocket message to client
        """
        if timestamp is None:
            timestamp = time.time() * 1000
         
        encoding = None
        
        if type is None:
            if isinstance(payload, str):
                type = 1
                encoding = 'utf-8'
            elif isinstance(payload, bytes):
                type = 2
            else:
                type = 0
                encoding = 'ascii'
         
        if self.log_level > 1 or (self.log_level > 0 and type <= 1):
            print(f"-- sending {Webserver.msg_type_str(type)} websocket message (type={type} size={len(payload)})")
            if type <= 1:
                pprint.pprint(payload)
                    
        if type == 0 and not isinstance(payload, str):  # json.dumps() might have already been called
            #print('sending JSON', payload)
            payload = json.dumps(payload)
            
        if not isinstance(payload, bytes):
            if encoding is not None:
                payload = bytes(payload, encoding=encoding)
            else:
                payload = bytes(payload)
                
        # do we even need this queue at all and can the websocket just send straight away?
        self.ws_queue.put(b''.join([
            #
            # 32-byte message header format:
            #
            #   0   uint64  message_id    (message_count_tx)
            #   8   uint64  timestamp     (milliseconds since Unix epoch)
            #   16  uint16  magic_number  (42)
            #   18  uint16  message_type  (0=json, 1=text, >=2 binary)
            #   20  uint32  payload_size  (in bytes)
            #   24  uint32  unused        (padding)
            #   28  uint32  unused        (padding)
            #
            struct.pack('!QQHHIII',
                self.msg_count_tx,
                int(timestamp),
                42, type,
                len(payload),
                0, 0,
            ),
            payload
        ]))

        self.msg_count_tx += 1
        
    def on_websocket(self, websocket):
        logging.info(f"-- new websocket connection from {websocket.remote_address}")

        # empty the queue from before the connection was made
        # (otherwise client will be flooded with old messages)
        # TODO implement self.connected so the ws_queue doesn't grow so large without webclient connected...
        while True:
            try:
                self.ws_queue.get(block=False)
            except queue.Empty:
                break
        
        #if self.msg_callback:
        #    self.msg_callback({'client_state': 'connected'}, 0, int(time.time()*1000))
            
        listener_thread = threading.Thread(target=self.websocket_listener, args=[websocket], daemon=True)
        listener_thread.start()

        while True:
            websocket.send(self.ws_queue.get()) #json.dumps(self.ws_queue.get()))
   
    def websocket_listener(self, websocket):
        logging.info(f"-- listening on websocket connection from {websocket.remote_address}")

        header_size = 32
            
        while True:
            msg = websocket.recv()
            
            if isinstance(msg, str):
                logging.warning(f'dropping text-mode websocket message from {websocket.remote_address} "{msg}"')
                continue
                
            if len(msg) <= header_size:
                logging.warning(f"dropping invalid websocket message from {websocket.remote_address} (size={len(msg)})")
                continue
                
            msg_id, timestamp, magic_number, msg_type, payload_size, _, _ = \
                struct.unpack_from('!QQHHIII', msg)
                
            if magic_number != 42:
                logging.warning(f"dropping invalid websocket message from {websocket.remote_address} (magic_number={magic_number} size={len(msg)})")
                continue

            if msg_id != self.msg_count_rx:
                logging.debug(f"recieved websocket message from {websocket.remote_address} with out-of-order ID {msg_id}  (last={self.msg_count_rx})")
                self.msg_count_rx = msg_id
                
            self.msg_count_rx += 1
            msgPayloadSize = len(msg) - header_size
            
            if payload_size != msgPayloadSize:
                logging.warning(f"recieved invalid websocket message from {websocket.remote_address} (payload_size={payload_size} actual={msgPayloadSize}");
            
            payload = msg[header_size:]
            
            if msg_type == 0:  # json
                payload = json.loads(payload)
            elif msg_type == 1:  # text
                payload = payload.decode('utf-8')

            if logging.getLogger().isEnabledFor(logging.DEBUG):
                logging.debug(f"-- recieved {Webserver.msg_type_str(msg_type)} websocket message from {websocket.remote_address} (type={msg_type} size={payload_size})")
                if msg_type <= 1:
                    pprint.pprint(payload)
                
            self.on_message(payload, msg_type, timestamp)
  
    @staticmethod
    def on_index():
        return flask.render_template(self.index)
    
    def send_chat_history(self, history):
        history = copy.deepcopy(history)
        
        def translate_web(text):
            text = text.replace('\n', '<br/>')
            return text
            
        for n in range(len(history)):
            for m in range(len(history[n])):
                history[n][m] = translate_web(history[n][m])
                
        #print("-- sending chat history", history)
        self.send_message({'chat_history': history})
    
    
    @staticmethod
    def msg_type_str(type):
        if type == 0:
            return "json"
        elif type == 1:
            return "text"
        else:
            return "binary"
    