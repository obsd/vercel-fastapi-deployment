from typing import Callable
from slack_sdk import WebClient
import re
from time import time
from fastapi import FastAPI, Response, __version__
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi_slackeventsapi import SlackEventManager
import logging
from pydantic import BaseModel
from gql import Client
from gql.transport.requests import RequestsHTTPTransport
from pdpyras import APISession
from cachetools import TTLCache
from common.utils import create_linear_ticket, fetch_slack_user_info, is_late_hour, send_slack_late_hour_notification, trigger_incident, notify_support_channel, default_pager_duty_user
from common.const import emails_exclude_list, general_channel_names
from dotenv import load_dotenv
import os

cache = TTLCache(maxsize=500, ttl=100)
class Item(BaseModel):
    data: dict

load_dotenv()
# secrets and constants
# slack
slack_signing_secret_inc = os.getenv('slack_signing_secret_inc', "")
slack_signing_secret = os.getenv('slack_signing_secret', "")
slack_client_token = os.getenv('slack_client_token', "")
slack_client_token_inc = os.getenv('slack_client_token_inc', "")
slack_support_channel_id = os.getenv('support_channel_id', "") 
# linear
linear_team_id = os.getenv('linear_team_id', "")
linear_auth_header = os.getenv('linear_auth_header', "")
# pager duty
pager_duty_api_key = os.getenv('pager_duty_api_key', "")
pager_duty_schedule_id = os.getenv('pager_duty_schedule_id', "")
pager_duty_escalation_policy_id = os.getenv('pager_duty_escalation_policy_id', "")
pager_duty_service_id = os.getenv('pager_duty_service_id', "")


app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# add custom middleware to fastapi that returns 200 for slack retries
@app.middleware("http")
async def check_if_slack_retries(request: Request, call_next: Callable):
    if request.headers.get('x-slack-retry-num'):
        logger.info('slack retry')
        return Response(status_code=200)
    else:
        response = await call_next(request)
        return response


slack_event_manger = SlackEventManager(singing_secret=slack_signing_secret,
                                       endpoint='/support/slack_events',
                                       app=app)

# slack_event_manger_inc = SlackEventManager(singing_secret=slack_signing_secret_inc,
#                                        endpoint='/support/slack_events_inc',
#                                        app=app)

pager_duty_api = APISession(
    pager_duty_api_key, # PagerDuty API token from secrets
    'PagerDuty' # Name for logging (optional)
    )

gql_logger = logging.getLogger('gql.transport.requests')
gql_logger.setLevel(logging.WARN)

# user email domains regex exclude list
emails_exclude_list_compiled = re.compile('|'.join(emails_exclude_list))

_slack_client = WebClient(
    token=slack_client_token)
_slack_client_inc = WebClient(
    token=slack_client_token_inc)

sample_transport = RequestsHTTPTransport(
    url="https://api.linear.app/graphql",
    use_json=True,
    headers={
        "Content-type": "application/json",
        "Authorization": linear_auth_header,
    },
    verify=False,
    retries=3,
)
linear_client = Client(
        transport=sample_transport, fetch_schema_from_transport=True
    )


def get_message_id(message):
    message_id = message['event']['client_msg_id']
    return message_id

def add_message_to_cache(message_id):
    cache[message_id] = True 

# @slack_event_manger_inc.on('message')
# async def message_sent_inc(event_data):
#     await _message_sent(event_data, _slack_client_inc)

@slack_event_manger.on('message')
async def message_sent(event_data):
    await _message_sent(event_data, _slack_client)

async def _message_sent(event_data, slack_client):

    message_id = get_message_id(event_data)
    if message_id in cache:
        logger.info('Message already in cache')
        return
    else:
        add_message_to_cache(message_id)

    try:
        message_text = event_data['event']['text']
    except KeyError as e:
        logger.error(
            "Error fetching conversations: {} {}".format(event_data, e))
        return
    try:
        user_obj = fetch_slack_user_info(slack_client=slack_client, user_id=event_data['event']['user']) # type: ignore
        if user_obj is None:
            logger.error("Error fetching user data: {}".format(event_data))
            return
        user_email = user_obj['profile']['email']
        if emails_exclude_list_compiled.search(user_email):
            logger.info('Email excluded')
            return
        else:
            logger.info('Email not excluded')
    except KeyError as e:
        logger.error("Error fetching user data: {}".format(e))
        user_obj = {'user_email': "user_email", 'profile': {'email': "no user found", 'real_name': "no user found"}}
    try:
        is_ext_shared_channel = event_data['is_ext_shared_channel']
    except KeyError as e:
        is_ext_shared_channel = False
    link_to_message = slack_client.chat_getPermalink(
        channel=event_data['event']['channel'],
        message_ts=event_data['event']['ts']
    )
    channel_id = event_data['event']['channel']
    channel_obj = slack_client.conversations_info(
        channel=channel_id
    )

    channel_name = channel_obj['channel']['name']
    if channel_name in general_channel_names:
        if '?' not in message_text:
            logger.info('Message without question mark in general channel')
            return

    username = user_obj['profile']['real_name']
    user_email = user_obj['profile']['email']
    link = link_to_message['permalink']
    # get user organization
    title = f"Support message from {username} on {channel_name}"
    details = f"Link to message {link} \nMessage content:\n{message_text}"
    if is_ext_shared_channel:
        details += "\nThis is an external shared channel"
    
    # get active oncall from pagerduty
    oncalls = pager_duty_api.iter_all( # type: ignore
        'oncalls', # method
        {
            #"include[]": "users", # including users doesn't give us the contact details
            "schedule_ids[]": pager_duty_schedule_id,
            "escalation_policy_ids[]": pager_duty_escalation_policy_id
        } #params
    )
    oncall_id = ""
    on_call_email = default_pager_duty_user
    if oncalls:
        for oncall in oncalls:
            oncall_id = oncall.get('user').get('id')
            pd_user = pager_duty_api.jget(f"/users/{oncall_id}") # type: ignore
            # print(pd_user)
            on_call_email = pd_user.get('user').get('email')
            break


    link_to_ticket = await create_linear_ticket(linear_client=linear_client, linear_team_id=linear_team_id, assignee_email=on_call_email, title=title, details=details)
    # notify in slack's support channel
    notify_support_channel(slack_client=_slack_client_inc, support_channel_id=slack_support_channel_id, link=link, username=username, email=user_email, source_name=channel_name, link_to_ticket=link_to_ticket) # type: ignore
    
    if is_late_hour():
        send_slack_late_hour_notification(slack_client=slack_client, channel_id=channel_id, channel_name=channel_name)

        





# http://ec2-18-216-212-68.us-east-2.compute.amazonaws.com:8000/support/slack_events
@app.post('/support/slack_events_old')
async def enable_slack_events(request: Request):
    """
    """
    print("request data:", request.headers)
    print("request data:", request.query_params)
    body = await request.json()
    print("request data:", body)
    return body.get('challenge')

# slack commands
@app.post('/support/commands/assign_org')
async def assign_org(token: str = Form(...), text: str = Form(...), channel_id: str = Form(...), user_id: str = Form(...), response_url: str = Form(...), trigger_id: str = Form(...)):
    """
    """
    print("JSON data:", token, text)
    # assign channel to org
    return {"message": "JSON data received and printed"}


@app.post('/support/commands/assign_user_to_org')
async def assign_usr_to_org(token: str = Form(...), text: str = Form(...), channel_id: str = Form(...), user_id: str = Form(...), response_url: str = Form(...), trigger_id: str = Form(...)):
    """
    """
    print("JSON data:", token, text)
    # assign user to org
    return {"message": "JSON data received and printed"}

# slack interactive
@app.post('/support/slack-interactive')
async def slack_interactive(request: Request):
    """
    """
    body = await request.body()
    if 'escalate_to_pagerduty' in body.decode():
        trigger_incident(pager_duty_api_key=pager_duty_api_key, pager_duty_service_id=pager_duty_service_id)
    print("request data:", body)
    return {"message": "JSON data received and printed"}

# github webhook is turned off
# @app.post('/support/github_wh')
# async def assign_org(request: Request):
#     """
#     """
#     print("request data:", request.headers)
#     print("request data:", request.query_params)
#     body = await request.json()
#     gh_event = request.headers['x-github-event']
#     print("request data:", body)
#     if (gh_event == 'issue_comment'):
#         if (body['action'] == 'created'):
#             print(
#                 f"new comment {body['comment']['body']} on {body['issue']['title']}")
#     if (gh_event == 'issues'):
#         if (body['action'] == 'opened'):
#             print(f"new issue {body['issue']['body']}")
#     else:
#         print(gh_event)
#         print(body.keys())

#     # assign user to org
#     return {"message": "JSON data received and printed"}

app.mount("/static", StaticFiles(directory="static"), name="static")

html = f"""
<!DOCTYPE html>
<html>
    <head>
        <title>FastAPI on Vercel</title>
        <link rel="icon" href="/static/favicon.ico" type="image/x-icon" />
    </head>
</html>
"""

@app.get("/")
async def root():
    return HTMLResponse(html)

@app.get('/ping')
async def hello():
    return {'res': 'pong', 'version': __version__, "time": time()}


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0')
