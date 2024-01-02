import logging
from dotenv import load_dotenv
import os
import requests
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.communication.email import EmailClient
from openai import AzureOpenAI
from datetime import datetime


def get_stop_id(name):
    logging.info(f'Getting stop id for {name}...')
    uri = f'{journeyplanner_base_uri}/location?input={name}&format=json'
    response = requests.get(uri)
    response_dict = response.json()
    stop_id = response_dict['LocationList']['StopLocation'][0]['id'] # Take first result
    return stop_id


if __name__ == '__main__':

    # Configure logging to print to stdout
    logging.basicConfig(level=logging.INFO)

    logging.info('Loading environment variables...')
    load_dotenv()
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
        api_version="2023-10-01-preview"
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

        # Construct chat messages
        joined_messages = '\n'.join(messages)
        system_message = f'''You are CommuteAI, a helpful assistant that writes alerts for commuters. If the traffic
                messages indicate an issue with the user\'s route, reply with a summary of the
                relevant messages to send as an email alert. Start your message with "Hi {user_name}".
                Follow these rules:
                1. Only include alerts that are directly relevant to the user at their origin, destination,
                or intermediary stops.
                2. Only inform the user about changes that began in the past few days. Do not include alerts that
                about changes that have been ongoing for longer. Today is {datetime.today().strftime("%A")}, {datetime.today().date()}.
                3. Consider, why the alert is affecting the user\'s commute and to what effect.
                4. Reply "N/A" if none of the alerts are immediately relevant.
                
                EXAMPLE 1:
                I plan to go from Brown Street to Central Station. I usually use these lines: 1, 2, 4. I go via Madison Boulevard.
                
                MESSAGES:
                Due to construction, the 1 will not stop at Brown Street. Use the 2 instead.

                ALERT:
                Hi {user_name}, I found these alerts that may affect your commute:
                Due to construction, the 1 will not stop at Brown Street. Use the 2 instead.

                
                EXAMPLE 2:
                I plan to go from Brown Street to Central Station. I usually use these lines: 1, 2, 4. I go via Madison Boulevard.

                MESSAGES:
                Due to construction, the 3 will not stop at Brown Street.

                ALERT:
                N/A

                
                EXAMPLE 3:
                I plan to go from Brown Street to Central Station. I usually use these lines: 1, 2, 4. I go via Madison Boulevard.

                MESSAGES:
                Due to construction, the 1 will not stop at Brown Street between November 2023 and March 2024.

                ALERT:
                N/A


                EXAMPLE 4:
                I plan to go from Brown Street to Central Station. I usually use these lines: 1, 2, 4. I go via Madison Boulevard.

                MESSAGES:
                Due to construction, the 1 will not stop at Lilly Street.

                ALERT:
                N/A
                '''
        via_line = f' I plan to go via {via_names}.' if via_names else ''
        user_message = f'''I am going from {origin_name} to {dest_name}.{via_line} I usually use these
                lines: {lines}. 
                \n\MESSAGES:
                \n{joined_messages}'''
        response = client.chat.completions.create(
            model=aoai_deployment_name,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message}
            ],
            max_tokens=300
        )
        alert = response.choices[0].message.content
        logging.info(f'Summary: {alert}')

        if alert == 'N/A':
            logging.info('No relevant issues found. Terminating.')
        else:
            logging.info('Sending email...')
            email = {
                "content": {
                    "subject": "Commute Alert for today",
                    "plainText": alert
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