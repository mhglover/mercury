"""functions for websocket manipulation"""
import json
import asyncio
import logging
from helpers import feelabout

# Assuming active_websockets is a dictionary mapping user IDs to WebSocket connection objects
# Initialize a dictionary for user message queues
user_message_queues = {}

def get_user_queue(user_id):
    """get the user's message queue, create it if necessary"""
    if user_id not in user_message_queues:
        user_message_queues[user_id] = asyncio.Queue()
    return user_message_queues[user_id]

active_websockets = {}

async def default_message_processor(user, message):
    # no-op, just log the message
    message_data = json.loads(message)
    logging.info("default_message_processor - no action for %s: %s", user.displayname, message_data)


async def send_webdata(w):
    """send the webdata to the websocket"""
    logging.info("sending webdata to user: %s", w.user.displayname)
    # logging.debug("sending webdata to websocket: %s\n%s", 
    #              w.user.displayname, pformat(w.to_dict())) 
    
    r = w.ratings[0].rating
    n = w.nextup.rating
    u = f"/track/{w.nextup.track_id}/rate/"
    
    data = {"update": [
        {"id": "currently_playing", "value": w.track.trackname, "class": feelabout(r)},
        {"id": "currently_downrate", "href": u + str(r-1)},
        {"id": "currently_uprate", "href": u + str(r+1)},
        {"id": "nextup", "value": w.nextup.trackname, "class": feelabout(n)},
        {"id": "nextup_downrate", "href": u + str(n+1)},
        {"id": "nextup_uprate", "href":  u + str(n+1)}
    ]}
    
    socket = active_websockets.get(w.user.id)
    #is socket still active
    if socket:
        await socket.send(json.dumps(data))
    else:
        logging.error("no livesocket not found for user: %s", w.user.displayname)


async def queue_webuser_updates(user_id: int, updates: list):
    """send multiple updates to the user's queue, to be handled by handle_websocket
    
    arguments: 
        user_id: int - the user's id
        updates: list - a list of dicts, each dict contains:
            id: str - the id of the element to update
            attribute: str - the attribute to update
            value: str - the new value for the attribute
        
        no return value
    """
    
    user_queue = get_user_queue(user_id)
    if not user_queue:
        logging.error("queue_webuser_updates - no user_queue found for user: %s", user_id)
        return

    # check to be sure the updates are in the proper format
    # it must have an 'updates' key, which is a list of dicts
    if not isinstance(updates, dict):
        logging.error("queue_webuser_updates - updates not a dict: %s", updates)
        return
    if not 'update' in updates:
        logging.error("queue_webuser_updates - updates missing 'updates' key: %s", updates)
        return
    if not isinstance(updates['update'], list):
        logging.error("queue_webuser_updates - updates['updates'] not a list: %s",
                      updates['update'])
        return
    
    try:
        logging.debug("queue_webuser_updates - sending updates to user: %s", user_id)
        await user_queue.put(json.dumps(updates))
        
    except RuntimeError as e:
        logging.error("queue_webuser_updates - RuntimeError sending update to user: %s", e)
    
    except Exception as e:
        logging.error("queue_webuser_updates - error sending update to user: %s", e)

