import base64
import datetime
import os
import re
import json
import traceback
import pandas as pd
from paramiko import SSHClient, AutoAddPolicy
from scp import SCPClient
from google.cloud import bigquery
from google.cloud import secretmanager
from linebot import LineBotApi
from linebot.models import TextSendMessage
from linebot.exceptions import LineBotApiError

project_id = os.environ.get('PROJECT_ID')
detaset_id = os.environ.get('DATASET_ID')

def access_secret_version(project_id, secret_name, secret_ver='latest'):
    client = secretmanager.SecretManagerServiceClient()
    name = client.secret_version_path(project_id, secret_name, secret_ver)
    response = client.access_secret_version(name=name)
    return response.payload.data.decode('UTF-8')

def LINE_notification(message):
    channel_access_token = access_secret_version(project_id, 'CHANNEL_ACCESS_TOKEN')
    user_id = access_secret_version(project_id, 'USER_ID')

    line_bot_api = LineBotApi(channel_access_token)

    try:
        line_bot_api.push_message(user_id, TextSendMessage(text=message))

    except LineBotApiError as e:
        raise(e)

def ssh_get_log_file(day):
    port = access_secret_version(project_id, 'PORTS')
    username = access_secret_version(project_id, 'USERNAME')
    password = access_secret_version(project_id, 'PASSWORD')

    with SSHClient() as ssh:
        ssh.set_missing_host_key_policy(AutoAddPolicy())

        ssh.connect(hostname='yuya-hanzawa.com', 
                    port=port, 
                    username=username,
                    password=password
                    )
  
        with SCPClient(ssh.get_transport()) as scp:
            scp.get(f'/var/log/nginx/access.log-{day:%Y%m%d}', '/tmp')

    with open(f'/tmp/access.log-{day:%Y%m%d}', encoding='utf-8', errors='ignore') as f:
        # この読み取り方法だと一部のデータが想定と違う形で読み込まれる。原因を確認中。
        # df = pd.read_json(log, orient='records', lines=True)
        df = pd.DataFrame(index=[])
        for line in f:
            data = json.loads(line)
            df = df.append(data, ignore_index=True)

        for column in df.columns:
            df[column] = df[column].astype(str)

    return df

def main(event, context):
    """Background Cloud Function to be triggered by Pub/Sub.
    Args:
         event (dict):  The dictionary with data specific to this type of event. The `@type` field maps to `type.googleapis.com/google.pubsub.v1.PubsubMessage`.
                        The `data` field maps to the PubsubMessage data in a base64-encoded string. 
                        The `attributes` field maps to the PubsubMessage attributes if any is present.

         context (google.cloud.functions.Context): Metadata of triggering event including `event_id` which maps to the PubsubMessage messageId, 
                                                   `timestamp` which maps to the PubsubMessage publishTime, `event_type` which maps to `google.pubsub.topic.publish`, 
                                                   and `resource` which is a dictionary that describes the service API endpoint pubsub.googleapis.com, 
                                                   the triggering topic's name, and the triggering event type `type.googleapis.com/google.pubsub.v1.PubsubMessage`.
    Returns:
        None. The output is written to Cloud Logging.
    """
    if re.match('([0-9]{4})-([0-9]{2})-([0-9]{2})', base64.b64decode(event['data']).decode('utf-8')):
        day = datetime.datetime.strptime(base64.b64decode(event['data']).decode('utf-8'), '%Y-%m-%d')

    else:
        day = datetime.datetime.now() - datetime.timedelta(days=1)

    try:
        df = ssh_get_log_file(day)

    except Exception as e:
        LINE_notification(f"Error occurred: {traceback.format_exc()}")
        raise(e)

    try:
        bq = bigquery.Client(project=project_id)
        dataset = bq.dataset(detaset_id)

        job = bq.load_table_from_dataframe(
            df,
            dataset.table(f'access_log-{day:%Y%m%d}'),
            job_config=bigquery.LoadJobConfig(
                schema='schema/schema.json'
            )
        )

        job.result()

        LINE_notification("Successful")

    except Exception as e:
        LINE_notification(f"Error occurred: {traceback.format_exc()}")
        raise(e)
