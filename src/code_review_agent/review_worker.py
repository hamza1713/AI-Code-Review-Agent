"""Internal subprocess entry point; never evaluates arbitrary operation names."""
import json
import sys
from pathlib import Path
from code_review_agent.review_executor import decode_inputs


def execute(operation, payload):
    if operation == 'review':
        from code_review_agent.review_service import ReviewService
        return ReviewService.execute_review(**payload).model_dump()
    if operation == 'bot':
        from code_review_agent.bot import CommandRouter
        from dataclasses import asdict
        return asdict(CommandRouter.dispatch(**payload))
    if operation == 'webhook':
        from code_review_agent.webhook_queue import execute_platform_review
        return execute_platform_review(**payload)
    raise ValueError('Unsupported worker operation')


def main():
    request_file, result_file = map(Path, sys.argv[1:3])
    request = json.loads(request_file.read_text(encoding='utf-8'))
    try:
        result = {'result': execute(request['operation'], decode_inputs(request['payload']))}
    except Exception as error:
        from code_review_agent.review_service import InputValidationError, RateLimitExceeded
        if isinstance(error, InputValidationError):
            result = {'error': str(error), 'status': 400}
        elif isinstance(error, RateLimitExceeded):
            result = {'error': 'Rate limit exceeded.', 'status': 429}
        else:
            result = {'error': 'Review execution failed. Check server logs and provider configuration.', 'status': 500}
    temporary = result_file.with_suffix('.tmp')
    temporary.write_text(json.dumps(result), encoding='utf-8')
    temporary.replace(result_file)

if __name__ == '__main__':
    main()
