"""
Name: post_to_queue_and_trigger_step_function.py

Description: Receives an events from an SQS queue, translates to get_current_archive_list's input format,
sends it to another queue, then triggers the internal report step function.
"""
import json
import os
from typing import Any, Dict, TypeVar

import boto3
import fastjsonschema

# noinspection PyPackageRequirements
from cumulus_logger import CumulusLogger

import sqs_library
from sqs_library import retry_error

OS_ENVIRON_TARGET_QUEUE_URL_KEY = "TARGET_QUEUE_URL"
OS_ENVIRON_STEP_FUNCTION_ARN_KEY = "STEP_FUNCTION_ARN"

OUTPUT_REPORT_BUCKET_REGION_KEY = "reportBucketRegion"
OUTPUT_REPORT_BUCKET_NAME_KEY = "reportBucketName"
OUTPUT_MANIFEST_KEY_KEY = "manifestKey"

RT = TypeVar("RT")  # return type

LOGGER = CumulusLogger(name="ORCA")
# Generating schema validators can take time, so do it once and reuse.
try:
    with open("schemas/input.json", "r") as raw_schema:
        _INPUT_VALIDATE = fastjsonschema.compile(json.loads(raw_schema.read()))
except Exception as ex:
    LOGGER.error(f"Could not build schema validator: {ex}")
    raise

try:
    with open("schemas/body.json", "r") as raw_schema:
        _BODY_VALIDATE = fastjsonschema.compile(json.loads(raw_schema.read()))
except Exception as ex:
    LOGGER.error(f"Could not build schema validator: {ex}")
    raise


def process_record(
    record: Dict[str, Any],
    target_queue_url: str,
    step_function_arn: str,
) -> None:
    """
    Central method for translating record to pass along and triggering step function.

    Args:
        record: The record to post.
        target_queue_url: The url of the queue to post the records to.
        step_function_arn: The arn of the step function to trigger.
    """
    new_body = translate_record_body(record["body"])
    sqs_library.post_to_fifo_queue(target_queue_url, new_body)
    trigger_step_function(step_function_arn)


def translate_record_body(body: str) -> Dict[str, Any]:
    """
    Translates the SQS body into the format expected by the get_current_archive_list queue.
    Args:
        body: The string to convert.

    Returns:
        See get_current_archive_list/schemas/input.json for details.
    """
    body = json.loads(body)
    _BODY_VALIDATE(body)

    new_body = {
        OUTPUT_REPORT_BUCKET_REGION_KEY: body["awsRegion"],
        OUTPUT_REPORT_BUCKET_NAME_KEY: body["s3"]["bucket"]["name"],
        OUTPUT_MANIFEST_KEY_KEY: body["s3"]["object"]["key"],
    }
    return new_body


@retry_error()
def trigger_step_function(
    step_function_arn: str,
) -> None:
    """
    Triggers state machine with retries.
    Args:
        step_function_arn: The arn of the step function to trigger.
    """
    boto3.client("stepfunctions").start_execution(stateMachineArn=step_function_arn)


def handler(event: Dict[str, Any], context) -> None:
    """
    Lambda handler.
    Receives an events from an SQS queue, translates to get_current_archive_list's input format,
    sends it to another queue, then triggers the internal report step function.
    Args:
        event: See input.json for details.
        context: An object passed through by AWS. Used for tracking.
    Environment Vars:
        TARGET_QUEUE_URL (string): The URL of the SQS queue the job came from.
    Returns: See output.json for details.
    """
    LOGGER.setMetadata(event, context)

    _INPUT_VALIDATE(event)

    try:
        target_queue_url = str(os.environ[OS_ENVIRON_TARGET_QUEUE_URL_KEY])
    except KeyError as key_error:
        LOGGER.error(f"{OS_ENVIRON_TARGET_QUEUE_URL_KEY} environment value not found.")
        raise key_error
    try:
        step_function_arn = str(os.environ[OS_ENVIRON_STEP_FUNCTION_ARN_KEY])
    except KeyError as key_error:
        LOGGER.error(f"{OS_ENVIRON_STEP_FUNCTION_ARN_KEY} environment value not found.")
        raise key_error

    records = event["Records"]
    if len(records) != 1:
        raise ValueError(f"Must be passed a single record. Was {len(records)}")
    record = records[0]

    process_record(record, target_queue_url, step_function_arn)
