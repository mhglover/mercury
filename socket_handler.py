"""the socket handler for the websockets"""
import asyncio
import json
import logging
from raters import quickrate
from socket_funcs import active_websockets, default_message_processor, get_user_queue, user_message_queues


async def handle_websocket(websocket, user, message_processors=None, sleep=0.1):
    # grab the user's message queue
    user_queue = get_user_queue(user.id)
    # add the new websocket to the active_websockets dictionary
    active_websockets[user.id] = websocket
    logging.info("new websocket connection stored for user: %s", user.displayname)
    
    if not message_processors:
        message_processors = [default_message_processor, quickrate]

    try:
        while True:
            
            # check for messages in the queue
            queue_message = await user_queue.get()
            logging.debug("handle_websocket sending message to %s: %s", 
                          user.displayname, queue_message)
            await websocket.send(queue_message)
            user_queue.task_done()
        
            # check for messages from the websocket
            logging.debug("Checking for messages from %s", user.displayname)
            message = await websocket.receive()
            if message:
                logging.debug("handle_websocket received message from %s: %s", user.displayname, message)
                message_data = json.loads(message)
        
                for processor, _ in message_data.items():
                    processor_func = globals().get(processor)
                    
                    if processor_func and callable(processor_func):
                        await processor_func(user, message)
                    else:
                        logging.error("handle_websocket - no message_processor found for %s: %s", 
                                    user.displayname, str(processor))

            await asyncio.sleep(sleep)
    except asyncio.CancelledError:
        logging.info("handle_websocket cancelled for user: %s", user.displayname)
    finally:
        logging.info("handle_websocket closing for user: %s", user.displayname)
        del active_websockets[user.id]
        del user_message_queues[user.id]
