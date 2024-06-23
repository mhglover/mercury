"""functions for user manipulations"""
import logging
import pickle
from models import User

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace

async def getuser(cred, user):
    """fetch user details
    cred - spotify credentials object
    userid - either a user object, a user.id, or a user.spotifyid
    
    returns: user object, spotify token
    """
    if isinstance(user, str):
        try:
            user = await User.get(spotifyid=user)
        except Exception as e:
            logging.error("getuser exception 1 fetching user\n%s", e)
    
    elif isinstance(user, int):
        try:
            user = await User.get(id=user)
        except Exception as e:
            logging.error("getuser exception 2 fetching user\n%s", e)
    
    if not isinstance(user, User):
        logging.error("getuser unable to find user %s", user)
        user = None
        token = None
    else:
        token = pickle.loads(user.token)
        if token.is_expiring:
            try:
                token = cred.refresh(token)
            except Exception as e:
                logging.error("getuser exception refreshing token\n%s", e)
            user.token = pickle.dumps(token)
            await user.save()

    return user, token


async def getactiveusers():
    """fetch details for the active users
    
    returns: list of Users
    """
    users = await User.exclude(status="inactive")
    return users
