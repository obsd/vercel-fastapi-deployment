import datetime
import logging

from gql import gql, Client
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)
default_pager_duty_user = "asaf@permit.io"

def is_late_hour():
    now = datetime.datetime.now()
    if now.hour > 2 or now.hour < 7:
        return True
    return False

def send_slack_late_hour_notification(slack_client, channel_id):
                slack_client.chat_postMessage(
            channel=channel_id,
            text=f"Hi, thanks for reaching out :innocent:; it seems most of the team is AFK(:sleeping:) at the moment- so please expect a delay in response. If this is an emergency you can click the button below to escalate the call  (:warning: this would probably wake someone up).",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "emoji": True,
                        "text": f"Hi, thanks for reaching out :innocent:; it seems most of the team is AFK(:sleeping:) at the moment- so please expect a delay in response. If this is an emergency you can click the button below to escalate the call  (:warning: this would probably wake someone up)."
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": ":bell: Escalate! This is urgent!"
                            },
                            "style": "danger",
                            "value": "escalate_to_pagerduty",
                        }
                    ]
                }
            ]
        )

def get_linear_active_cycle(linear_client, team_id):
    get_active_cycle_query = gql("""
        query ($teamId: String!) {
            team(id: $teamId) {
                activeCycle {
                    id
                    name
                }
            }
        }
        """) 
    variables = {
        "teamId": team_id
    }
    active_cycle_data = linear_client.execute(get_active_cycle_query, variable_values=variables)
    cycle_id = active_cycle_data['team']['activeCycle']['id']
    return cycle_id

async def create_linear_ticket(linear_client, linear_team_id, assignee_email, title, details, org_key="", labels=[]):
    try:
        cycle_id = get_linear_active_cycle(linear_client, linear_team_id)
        # get assignee id by email
        get_assignee_id_query = gql("""
            query ($teamId: String!, $email: String!) {
                team(id: $teamId) {
                    members(filter: {email: {eq: $email}}) {
                        nodes {
                            id
                            email
                        }
                    }
                }
            }
            """)
        
        if not assignee_email:
            assignee_email = default_pager_duty_user
        variables = {
            "teamId": linear_team_id,
            "email": assignee_email 
            
        }
        assignee_data = linear_client.execute(get_assignee_id_query, variable_values=variables)
        assignee_id = assignee_data['team']['members']['nodes'][0]['id']
        # add assignee by email
        graphQLQuery = gql("""
            mutation ($teamId: String!, $title: String!, $description: String!, $cycleId: String!, $assigneeId: String!) {
                issueCreate(input: {
                    teamId: $teamId
                    title: $title
                    description: $description
                    cycleId: $cycleId
                    assigneeId: $assigneeId
                }) {
                    issue {
                    id
                    url
                    title
                    description
                    descriptionData
                    }
                }
            }
            """)
        variables = {
            "teamId": linear_team_id,
            "title": title,
            "description": details,
            "cycleId": cycle_id,
            "assigneeId": assignee_id,
        }
        data = linear_client.execute(graphQLQuery, variable_values=variables)
        return data['issueCreate']['issue']['url']

        
    except Exception as e:
        logger.error(f"Got an error: {e}")


def trigger_incident(pager_duty_api_key, pager_duty_service_id):
    """Triggers an incident via the V2 REST API using sample data."""
    import json
    import requests
    url = 'https://api.pagerduty.com/incidents'
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/vnd.pagerduty+json;version=2',
        'Authorization': 'Token token={token}'.format(token=pager_duty_api_key),
        'From': "Permit support automation <help@permit.io>"
    }

    payload = {
        "incident": {
            "type": "incident",
            "title": "Client requested urgent support",
            "service": {
                "id": pager_duty_service_id,
                "type": "service_reference"
            },
            "body": {
                "type": "incident_body",
                "details": "Client requested urgent support. go to https://permit-inc.slack.com/archives/C03CSBD6DEX to see the details"
            }
          }
        }

    r = requests.post(url, headers=headers, data=json.dumps(payload))

    print('Status Code: {code}'.format(code=r.status_code))
    print(r.json())

def notify_support_channel(slack_client, support_channel_id, link, username= "", email="", source_name="", link_to_ticket=""):
    try:
        message_text = f"New support message sent by {username}({email}) on {source_name} {link}"
        if link_to_ticket:
            message_text += f" - <{link_to_ticket}|Link to Linear>"
        response = slack_client.chat_postMessage(
        channel=support_channel_id,
        text=message_text
    )
    except Exception as e:
        logger.error(f"Got an error: {e}")


def fetch_slack_user_info(slack_client, user_id):
    try:
        result = slack_client.users_info(
            user=user_id
        )
        logger.info(result.data['user']['real_name'])
        # print(result)

        return result.data['user']

    except SlackApiError as e:
        logger.error("Error fetching conversations: {}".format(e))

