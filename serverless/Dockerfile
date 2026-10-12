# One image serves both Lambdas. deploy.py picks the handler for each with
# ImageConfig.Command, so the splitter and renderer always run the same code.
# Build context is the serverless/ folder.
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Keep the serverless.* package path, so imports match the repo
COPY __init__.py ${LAMBDA_TASK_ROOT}/serverless/__init__.py
COPY common/ ${LAMBDA_TASK_ROOT}/serverless/common/
COPY splitter/ ${LAMBDA_TASK_ROOT}/serverless/splitter/
COPY renderer/ ${LAMBDA_TASK_ROOT}/serverless/renderer/

CMD ["serverless.splitter.handler.split_handler"]
