"""the socket handler for the websockets"""
import asyncio
import json
import logging
import inspect
from raters import quickrate
from socket_funcs import active_websockets, default_message_processor

async def handle_websocket(websocket, user, message_processors=None, sleep=0.1):
    # Store the WebSocket connection
    active_websockets[user.id] = websocket
    logging.info("new websocket connection stored for user: %s", user.displayname)
    if not message_processors:
        message_processors = [default_message_processor]

    while True:
        try:
            message = await websocket.receive()
            logging.debug("Message received from %s: %s", user.displayname, message)
        except Exception as e:
            logging.error("WebSocket Error: %s %s", user.displayname, e, exc_info=True)    
            break
        except asyncio.CancelledError:
            logging.info("Websocket task was cancelled. Cleaning up... %s", user.displayname)
            break
        
        message_data = json.loads(message)
        
        for processor, _ in message_data.items():
            processor_func = globals().get(processor)
            
            if processor_func and callable(processor_func):
                await processor_func(user, message)
            else:
                logging.error("handle_websocket - no message_processor found for %s: %s", 
                             user.displayname, str(processor))
            # else:
            #     logging.error("handle_websocket - no message_processor found for %s: %s", 
            #              user.displayname, str(processor))
                
        await asyncio.sleep(sleep)
    
    # Remove the WebSocket connection when done
    del active_websockets[user.id]