"""functions for websocket manipulation"""
import json
import asyncio
import logging
from helpers import feelabout

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


async def send_update(userid: int, element_id: str, attribute_type: str, value: str):
    socket = active_websockets[userid]
    logging.info("sending update to user: %s, %s=%s", userid, attribute_type, value)
    
    try:
        data = {"update": [
            {
                "id": element_id, 
                attribute_type: value
            }
        ]}
    except Exception as e:
        logging.error("error creating data for send_update: %s", e)
        return
    
    await socket.send(json.dumps(data))    
