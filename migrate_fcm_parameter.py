"""Copy an existing FCM secret to a standard Parameter Store SecureString."""
import argparse
import json

import boto3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', default='prod')
    parser.add_argument('--region', required=True)
    parser.add_argument('--profile')
    parser.add_argument('--secret-id', help='Existing secret name or ARN (defaults to the stage FCM secret)')
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    secret_id = args.secret_id or f'ambatu-chat/{args.stage}/fcm'
    response = session.client('secretsmanager').get_secret_value(SecretId=secret_id)
    if 'SecretString' not in response:
        raise SystemExit('The existing secret must contain a JSON SecretString.')
    try:
        credential = json.loads(response['SecretString'])
    except ValueError:
        raise SystemExit('The existing secret is not valid JSON.') from None
    if not isinstance(credential, dict):
        raise SystemExit('The existing secret must contain a JSON object.')
    value = json.dumps(credential, ensure_ascii=False, separators=(',', ':'))
    if len(value.encode('utf-8')) > 4096:
        raise SystemExit('The compacted credential exceeds the 4096-byte standard parameter limit.')

    name = f'/ambatu-chat/{args.stage}/fcm'
    session.client('ssm').put_parameter(Name=name, Value=value, Type='SecureString', Tier='Standard')
    print(f'Created {name} as a standard SecureString. The original secret was not changed.')


if __name__ == '__main__':
    main()
