import logging
import os
import requests
import json
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.communication.email import EmailClient
import azure.functions as func
from openai import AzureOpenAI
from datetime import datetime


def get_stop_id(name):
    logging.info(f'Getting stop id for {name}...')
    journeyplanner_base_uri = os.getenv('JOURNEYPLANNER_BASE_URI')
    uri = f'{journeyplanner_base_uri}/location?input={name}&format=json'
    response = requests.get(uri)
    response_dict = response.json()
    stop_id = response_dict['LocationList']['StopLocation'][0]['id'] # Take first result
    return stop_id


app = func.FunctionApp()

@app.timer_trigger(
        schedule="0 0 6 * * 1-5",
        arg_name="timer",
        run_on_startup=False,
        use_monitor=False)
def commute_alert(timer: func.TimerRequest) -> None:
    logging.info('Function triggered by timer.')
    logging.info('Loading environment variables...')
    origin_id = os.getenv('ORIGIN_ID')
    dest_id = os.getenv('DEST_ID')
    origin_name = os.getenv('ORIGIN_NAME')
    dest_name = os.getenv('DEST_NAME')
    via_names = os.getenv('VIA_NAMES')
    lines = os.getenv('LINES')
    journeyplanner_base_uri = os.getenv('JOURNEYPLANNER_BASE_URI')
    keyvault_uri = os.getenv('KEYVAULT_URI')
    aoai_uri = os.getenv('AOAI_URI')
    aoai_key_name = os.getenv('AOAI_KEY_NAME')
    aoai_deployment_name = os.getenv('AOAI_DEPLOYMENT_NAME')
    comms_conn_str_name = os.getenv('COMMS_CONN_STR_NAME')
    user_email = os.getenv('USER_EMAIL')
    system_email = os.getenv('SYSTEM_EMAIL')
    user_name = os.getenv('USER_NAME')

    logging.info('Getting default Azure credential...')
    az_credential = DefaultAzureCredential()

    logging.info('Getting secret from Key Vault...')
    secret_client = SecretClient(vault_url=keyvault_uri, credential=az_credential)
    aoai_key = secret_client.get_secret(aoai_key_name).value
    comms_connection_string = secret_client.get_secret(comms_conn_str_name).value

    logging.info('Getting OpenAI client...')
    client = AzureOpenAI(
        azure_endpoint = aoai_uri, 
        api_key=aoai_key,  
        api_version="2023-12-01-preview"
    )

    logging.info('Getting Communication Services client...')
    email_client = EmailClient.from_connection_string(comms_connection_string)

    # Get stop ids if not in environment variables
    if not origin_id:
        origin_id = get_stop_id(origin_name)
    if not dest_id:
        dest_id = get_stop_id(dest_name)

    logging.info('Getting trips from Journeyplanner API...')
    journeyplanner_uri = f'{journeyplanner_base_uri}/trip?originId={origin_id}&destId={dest_id}&format=json'
    response = requests.get(journeyplanner_uri)
    response_dict = response.json()
    trips = response_dict['TripList']['Trip']
    
    logging.info('Collecting messages...')
    messages = []
    for trip in trips:
        if isinstance(trip['Leg'], list):
            for leg in trip['Leg']:
                if leg.get('MessageList'):
                    for message in leg['MessageList']['Message']:
                        if message['Text']['$'] not in messages:
                            messages.append(message['Text']['$'])
        else:
            if trip['Leg'].get('MessageList'):
                for message in trip['Leg']['MessageList']['Message']:
                    if message['Text']['$'] not in messages:
                        messages.append(message['Text']['$'])

    logging.info(f'Found {len(messages)} messages.{" Terminating." if not len(messages) else ""}')
    if len(messages):

        # First LLM task: Classification
        joined_messages = '\n'.join(f'{i}: {message}' for i, message in enumerate(messages))
        system_message = f'''
                You are classifying public transport alerts based on their relevance to the
                user's commute. Use the following rules to determine if an alert is relevant:
                1. If the alert affects a stop that the user is using, it is relevant.
                2. If the alert affects a stop that the user is using, but the user is not using the line that the alert is for, it is not relevant.
                3. If the alert affects a stop that the user is not using, it is not relevant.
                4. If the alert affects a line that the user is not using, it is not relevant.
                5. If the alert affects a stop on the line that is neither origin nor destination but the user is going directly without transfers, it is not relevant.
                6. If the alert is about a situation that began longer than a week ago, it is not relevant.
                Consider the current date. Today is {datetime.today().strftime("%A")}, {datetime.today().date()}.
                Respond as a list of JSON objects with properties: id (int), relevant (bool), reason (str).

                ALERTS:
                {joined_messages}
                '''
        user_message = f'''
                I am going from {origin_name} to {dest_name}. I am going
                {('via' + via_names) if via_names else 'directly'}.
                I am usually using these lines: {lines}.
                '''
        response = client.chat.completions.create(
            model=aoai_deployment_name,
            # response_format={ "type": "json_object" },
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message}
            ],
            max_tokens=300
        )
        classified_messages = json.loads(response.choices[0].message.content)
        logging.info(f'Classified messages: {classified_messages}')

        # Second LLM task: Summarize
        relevant_messages = [messages[message['id']] for message in classified_messages if message['relevant']]
        relevant_messages_joined = '\n'.join(relevant_messages)
        if not len(relevant_messages):
            logging.info('No relevant issues found. Terminating.')
        else:
            system_message = f'''
                You are CommuteAI, a notification system that helps commuters get relevant alerts.
                Your task is to summarize public transportation alerts for the user's commute.
                Begin your summary with "Good morning, {user_name}".

                Alerts:
                {relevant_messages_joined}
            '''

            response = client.chat.completions.create(
                model=aoai_deployment_name,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message}
                ],
                max_tokens=300
            )
            summary = response.choices[0].message.content
            logging.info(f'Summary: {summary}')

            logging.info('Sending email...')
            email = {
                "content": {
                    "subject": "Commute Alert for today",
                    "plainText": summary
                },
                "recipients": {
                    "to": [
                        {
                            "address": user_email,
                            "displayName": user_name
                        }
                    ]
                },
                "senderAddress": system_email
            }

            poller = email_client.begin_send(email)
            logging.info(f'Result: {poller.result()}. Terminating.')