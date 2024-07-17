"""the socket handler for the websockets"""
#pylint: disable=unused-import

import asyncio
import json
import logging
import time
from raters import quickrate 
from socket_funcs import (
    active_websockets,
    default_message_processor,
    get_user_queue,
    user_message_queues
)


async def listener_handler(websocket, user):
    """handle the websocket connection"""
    
    logging.debug("listener_handler looping for receiving messages from %s", user.displayname)
    
    while True:
        try:
            message = await websocket.receive()
        except Exception as e:
            logging.error("listener_handler error for user %s: %s", user.displayname, e)
            break
        
        logging.debug("listener_handler received message from %s: %s", user.displayname, message)
        message_data = json.loads(message)
        
        for processor, _ in message_data.items():
            processor_func = globals().get(processor)
                    
            if processor_func and callable(processor_func):
                logging.debug("listener_handler - processing %s message for %s: %s",
                                    processor, user.displayname, message_data)
                await processor_func(user, message)
            else:
                logging.error("listener_handler - no message_processor found for %s: %s", 
                                    user.displayname, str(processor))


async def sender_handler(websocket, user):
    """send messages from the user's queue to the websocket"""
    loop = asyncio.get_running_loop()
    
    start_time = loop.time()  # Start timing
    user_queue = get_user_queue(user.id)
    end_time = loop.time()  # End timing
    logging.debug("sender_handler - got user queue in %s seconds", end_time - start_time)
    
    while True:
        if user_queue.empty():
            logging.debug("sender_handler - queue is empty, sleeping for 1 second")
            await asyncio.sleep(1)
            continue
        
        logging.debug("sender_handler - queue has %s items", user_queue.qsize())
        try:
            # Use get_nowait() to avoid blocking
            message = user_queue.get_nowait()
        except asyncio.QueueEmpty:
            # Break or handle empty queue appropriately
            logging.debug("sender_handler - really is empty, sleeping for 2 seconds")
            await asyncio.sleep(2)  # Prevent tight loop when queue is empty
            continue
        
        logging.debug("sender_handler - queue has %s items", user_queue.qsize())
        
        logging.debug("sender_handler sending queued message to %s: %s", user.displayname, message)
        
        messages = [x for x in json.loads(message)['update']]
        logging.debug("sender_handler - sending %s messages to %s", len(messages), user.displayname)
        for x in messages:
            logging.debug("id: %s, attribute: %s, value: %s", x['id'], x['attribute'], x['value']) 
        
        try:
            start_time = loop.time()
            logging.debug("sender_handler - sending message to %s", user.displayname)
            await websocket.send(message)
            end_time = loop.time()
        except Exception as e:
            logging.error("sender_handler error for user %s: %s", 
                          user.displayname, e)
            break
        
        logging.debug("sender_handler - sent message in %s seconds", 
                     end_time - start_time)

async def handle_websocket(websocket, user):
    # grab the user's message queue
    
    # add the new websocket to the active_websockets dictionary
    active_websockets[user.id] = websocket
    logging.debug("new websocket connection stored for user: %s", user.displayname)
    
    logging.debug("handle_websocket starting websocket listener loop for user: %s",
                    user.displayname)
    try: 
        listener_task = asyncio.create_task(listener_handler(websocket, user))
    except Exception as e:
        logging.error("listener_task error for user %s: %s", user.displayname, e)
    
    logging.debug("handle_websocket starting websocket sender loop for user: %s",
                    user.displayname)
    try:
        sender_task = asyncio.create_task(sender_handler(websocket, user))
    except Exception as e:
        logging.error("sender_task error for user %s: %s", user.displayname, e)
    
    try:
        _, pending = await asyncio.wait(
            [listener_task, sender_task],
            return_when=asyncio.FIRST_COMPLETED)
        
        for task in pending:
            task.cancel()
            
    except asyncio.CancelledError:
        logging.debug("handle_websocket cancelled for user %s", user.displayname)
                     
    except Exception as e:
        logging.error("handle_websocket error for user %s: %s", user.displayname, e)
    
    finally:
        if user.id in active_websockets:
            del active_websockets[user.id]
        logging.debug("handle_websocket websocket deleted: %s", user.displayname)
