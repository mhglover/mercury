"""functions for websocket manipulation"""
import logging
import json
from pprint import pformat
from helpers import feelabout


active_websockets = {}


async def send_webdata(w):
    """send the webdata to the websocket"""
    logging.info("sending webdata to user: %s", w.user.displayname)
    logging.debug("sending webdata to websocket: %s\n%s", 
                 w.user.displayname, pformat(w.to_dict())) 
    
    r = w.track.rating
    n = w.nextup.rating
    u = f"/track/{w.nextup.track_id}/rate/"
    
    data = [
        {"id": "currently_playing", "value": w.track.trackname, "class": feelabout(r)},
        {"id": "currently_downrate", "href": u + str(r-1)},
        {"id": "currently_uprate", "href": u + str(r+1)},
        {"id": "nextup", "value": w.nextup.trackname, "class": feelabout(n)},
        {"id": "nextup_downrate", "href": u + str(n+1)},
        {"id": "nextup_uprate", "href":  u + str(n+1)}
    ]
    
    socket = active_websockets.get(w.user.id)
    await socket.send(json.dumps(data))
