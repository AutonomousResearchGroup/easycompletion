import os
import time
import openai
import re
import json
import ast
import asyncio

from dotenv import load_dotenv
import tiktoken

# Load environment variables from .env file
load_dotenv()

from .constants import (
    EASYCOMPLETION_API_ENDPOINT,
    TEXT_MODEL,
    TEXT_MODEL_WINDOW,
    LONG_TEXT_MODEL,
    LONG_TEXT_MODEL_WINDOW,
    EASYCOMPLETION_API_KEY,
    DEFAULT_CHUNK_LENGTH,
    DEFAULT_MODEL_INFO,
    DEBUG,
    SUPPRESS_WARNINGS,
)

from .logger import log

from .prompt import count_tokens

openai.api_base = EASYCOMPLETION_API_ENDPOINT

def parse_arguments(arguments, debug=DEBUG):
    """
    Parses arguments that are expected to be either a JSON string, dictionary, or a list.

    Parameters:
        arguments (str or dict or list): Arguments in string or dictionary or list format.

    Returns:
        A dictionary or list of arguments if arguments are valid, None otherwise.

    Usage:
        arguments = parse_arguments('{"arg1": "value1", "arg2": "value2"}')
    """
    try:
        # Handle string inputs, remove any ellipsis from the string
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
    # If JSON decoding fails, try using ast.literal_eval
    except json.JSONDecodeError:
        try:
            arguments = ast.literal_eval(arguments)
        # If ast.literal_eval fails, remove line breaks and non-ASCII characters and try JSON decoding again
        except (ValueError, SyntaxError):
            try:
                arguments = re.sub(r"\.\.\.|\…", "", arguments)
                arguments = re.sub(r"[\r\n]+", "", arguments)
                arguments = re.sub(r"[^\x00-\x7F]+", "", arguments)
                arguments = json.loads(arguments)
            # If everything fails, try Python's eval function
            except Exception:
                try:
                    arguments = eval(arguments)
                except Exception:
                    arguments = None
    log(f"Arguments:\n{str(arguments)}", log=debug)
    return arguments


def validate_functions(response, functions, function_call, debug=DEBUG):
    """
    Validates if the function returned matches the intended function call.

    Parameters:
        response (dict): The response from the model.
        functions (list): A list of function definitions.
        function_call (dict or str): The expected function call.

    Returns:
        True if function call matches with the response, False otherwise.

    Usage:
        isValid = validate_functions(response, functions, function_call)
    """
    print('response')
    print(response)
    response_function_call = response["choices"][0]["message"].get(
        "function_call", None
    )
    if response_function_call is None:
        log(f"No function call in response\n{response}", type="error", log=debug)
        return False

    # If function_call is not "auto" and the name does not match with the response, return False
    if (
        function_call != "auto"
        and response_function_call["name"] != function_call["name"]
    ):
        log("Function call does not match", type="error", log=debug)
        return False

    # If function_call is "auto", extract the name from the response
    function_call_name = (
        function_call["name"]
        if function_call != "auto"
        else response_function_call["name"]
    )

    # Parse the arguments from the response
    arguments = parse_arguments(response_function_call["arguments"])

    # Get the function that matches the function name from the list of functions
    function = next(
        (item for item in functions if item["name"] == function_call_name), None
    )

    # If no matching function is found, return False
    if function is None:
        log(
            "No matching function found"
            + f"\nExpected function name:\n{str(function_call_name)}"
            + f"\n\nResponse:\n{str(response)}",
            type="error",
            log=debug,
        )
        return False

    # If arguments are None, return False
    if arguments is None:
        log(
            "Arguments are None"
            + f"\nExpected arguments:\n{str(function['parameters']['properties'].keys())}"
            + f"\n\nResponse function call:\n{str(response_function_call)}",
            type="error",
            log=debug,
        )
        #
        return False

    required_properties = function["parameters"].get("required", [])

    # Check that arguments.keys() contains all of the required properties
    if not all(
        required_property in arguments.keys()
        for required_property in required_properties
    ):
        log(
            "ERROR: Response did not contain all required properties.\n"
            + f"\nExpected keys:\n{str(function['parameters']['properties'].keys())}"
            + f"\n\nActual keys:\n{str(arguments.keys())}",
            type="error",
            log=debug,
        )

        return False

    log("Function call is valid", type="success", log=debug)
    return True


def is_long_model(model_name):
    return "16k" in model_name

def build_model_info(model_names, factor=0.75):
    return [
        (model_name,
         int(factor * (LONG_TEXT_MODEL_WINDOW if is_long_model(model_name) else TEXT_MODEL_WINDOW)))
        for model_name in model_names
    ]


def sanity_check(prompt, model=None, model_info=None, chunk_length=DEFAULT_CHUNK_LENGTH, api_key=EASYCOMPLETION_API_KEY, debug=DEBUG):
    # Validate the API key
    if not api_key.strip():
        return [], {"error": "Invalid OpenAI API key"}

    openai.api_key = api_key
    # Construct a model_info from legacy parameters
    if chunk_length not in (None, DEFAULT_CHUNK_LENGTH):
        log("Warning: deprecated use of chuck_length. Please use model_info.",
            str="warning", log=not SUPPRESS_WARNINGS)
    else:
        chunk_length = chunk_length or DEFAULT_CHUNK_LENGTH
    if model is not None:
        if model == TEXT_MODEL and chunk_length == DEFAULT_CHUNK_LENGTH:
            log("Warning: deprecated use of model, use model_info",
                str="warning", log=not SUPPRESS_WARNINGS)
            model_info = DEFAULT_MODEL_INFO
        else:
            log("Warning: deprecated use of model. Assuming long_model allowed. Use model_info otherwise.",
                str="warning", log=not SUPPRESS_WARNINGS)
            model_info = ((model, chunk_length), (LONG_TEXT_MODEL, LONG_TEXT_MODEL_WINDOW - DEFAULT_CHUNK_LENGTH))
    elif chunk_length != DEFAULT_CHUNK_LENGTH:
        log("Warning: deprecated use of chuck_length. Please use model_info.",
            str="warning", log=not SUPPRESS_WARNINGS)
        model_info = ((TEXT_MODEL, chunk_length), (LONG_TEXT_MODEL, LONG_TEXT_MODEL_WINDOW - chunk_length))
    else:
        model_info = model_info or DEFAULT_MODEL_INFO

    model_info = sorted(model_info, key=lambda i: i[1])

    # Names of long enough models
    models = []
    len_by_encoding = {}
    len_by_model = {}
    for model, chunk_length in model_info:
        encoding = tiktoken.encoding_for_model(model)
        if encoding not in len_by_encoding:
            # Count of tokens in the input text
            len_by_encoding[encoding] = count_tokens(prompt, model=model)
        len_by_model[model] = (token_len := len_by_encoding[encoding])
        if token_len <= chunk_length:
            models.append(model)

    # If text is too long even for long text model, return None
    if not models:
        print("Error: Message too long")
        return models, {
            "text": None,
            "usage": None,
            "finish_reason": None,
            "error": "Message too long",
        }

    if models[0] != model_info[0][0]:
        log("Warning: Message is long. Using larger models (to hide this message, set SUPPRESS_WARNINGS=1)",
            str="warning", log=not SUPPRESS_WARNINGS)

    total_tokens = len_by_model[models[0]]  # First appropriate model

    if isinstance(prompt, dict):
        for key, value in prompt.items():
            if value:
                log(f"Prompt {key} ({count_tokens(value, model=models[0])} tokens):\n{str(value)}", type="prompt", log=debug)
    else:
        log(f"Prompt ({total_tokens} tokens):\n{str(prompt)}", type="prompt", log=debug)

    return models, None

def do_chat_completion(
        messages, models, temperature=0.8, functions=None, function_call=None, model_failure_retries=5, debug=DEBUG):
    # Try to make a request for a specified number of times
    response = None
    model = models[0]
    for i in range(model_failure_retries):
        try:
            if functions is not None:
                response = openai.ChatCompletion.create(
                    model=model, messages=messages, temperature=temperature,
                    functions=functions, function_call=function_call,
                )
            else:
                response = openai.ChatCompletion.create(
                    model=model, messages=messages, temperature=temperature
                )
            log('response', log=debug)
            log(response, log=debug)
            break
        except Exception as e:
            log(f"OpenAI Error: {e}", type="error", log=debug)

    # Check if failed for length reasons.
    choices = response.get("choices", [])
    if choices and all(choice.get("finish_reason", None) == 'length' for choice in choices):
        models.pop(0)  # Side effect: Do not ever retry that model on that prompt
        if models:
            log("Failed because of length, trying next model", log=debug)
            return do_chat_completion(messages, models, temperature, functions, function_call, model_failure_retries, debug)
        return None, {
            "text": None,
            "usage": None,
            "finish_reason": 'length',
            "error": "Error: The prompt elicits too-long responses",
        }

    # TODO: Are there other reasons to try fallback models?

    # If response is not valid, print an error message and return None
    if (
        response is None
        or response["choices"] is None
        or response["choices"][0] is None
    ):
        return None, {
            "text": None,
            "usage": None,
            "finish_reason": None,
            "error": "Error: Could not get a successful response from OpenAI API",
        }
    return response, None

def chat_completion(
    messages,
    model_failure_retries=5,
    model=None,
    model_info=None,
    chunk_length=DEFAULT_CHUNK_LENGTH,
    api_key=EASYCOMPLETION_API_KEY,
    debug=DEBUG,
    temperature=0.0,
):
    """
    Function for sending chat messages and returning a chat response.

    Parameters:
        messages (str): Messages to send to the model. In the form {<role>: string, <content>: string} - roles are "user" and "assistant"
        model_failure_retries (int, optional): Number of retries if the request fails. Default is 5.
        model (str, optional): The model to use. Deprecated.
        chunk_length (int, optional): Maximum length of text chunk to process. Deprecated.
        model_info (List[Tuple[str, int]], optional): The list of models to use, and their respective chuck length. Default is the DEFAULT_MODEL_INFO defined in constants.py.
        api_key (str, optional): OpenAI API key. If not provided, it uses the one defined in constants.py.

    Returns:
        str: The response content from the model.

    Example:
        >>> text_completion("Hello, how are you?", model_failure_retries=3, model='gpt-3.5-turbo', chunk_length=1024, api_key='your_openai_api_key')
    """
    openai.api_key = api_key

    # Use the default model if no model is specified

    models, error = sanity_check(messages, model_info=model_info, model=model, chunk_length=chunk_length, api_key=api_key, debug=debug)
    if error:
        return error

    # Try to make a request for a specified number of times
    response, error = do_chat_completion(
        messages, models, temperature=temperature, model_failure_retries=model_failure_retries, debug=debug)

    if error:
        return error

    # Extract content from the response
    text = response["choices"][0]["message"]["content"]
    finish_reason = response["choices"][0]["finish_reason"]
    usage = response["usage"]

    return {
        "text": text,
        "usage": usage,
        "finish_reason": finish_reason,
        "error": None,
    }


async def chat_completion_async(
    messages,
    model_failure_retries=5,
    model=None,
    model_info=None,
    chunk_length=DEFAULT_CHUNK_LENGTH,
    api_key=EASYCOMPLETION_API_KEY,
    debug=DEBUG,
    temperature=0.0,
):
    """
    Function for sending chat messages and returning a chat response.

    Parameters:
        messages (str): Messages to send to the model. In the form {<role>: string, <content>: string} - roles are "user" and "assistant"
        model_failure_retries (int, optional): Number of retries if the request fails. Default is 5.
        model (str, optional): The model to use. Deprecated.
        chunk_length (int, optional): Maximum length of text chunk to process. Deprecated.
        model_info (List[Tuple[str, int]], optional): The list of models to use, and their respective chuck length. Default is the DEFAULT_MODEL_INFO defined in constants.py.
        api_key (str, optional): OpenAI API key. If not provided, it uses the one defined in constants.py.

    Returns:
        str: The response content from the model.

    Example:
        >>> text_completion("Hello, how are you?", model_failure_retries=3, model='gpt-3.5-turbo', chunk_length=1024, api_key='your_openai_api_key')
    """

    # Use the default model if no model is specified
    model = model or TEXT_MODEL
    models, error = sanity_check(messages, model_info=model_info, model=model, chunk_length=chunk_length, api_key=api_key, debug=debug)
    if error:
        return error

    # Try to make a request for a specified number of times
    response, error = await asyncio.to_thread(lambda: do_chat_completion(
        messages, models, temperature=temperature, model_failure_retries=model_failure_retries, debug=debug))

    if error:
        return error

    # Extract content from the response
    text = response["choices"][0]["message"]["content"]
    finish_reason = response["choices"][0]["finish_reason"]
    usage = response["usage"]

    return {
        "text": text,
        "usage": usage,
        "finish_reason": finish_reason,
        "error": None,
    }


def text_completion(
    text,
    model_failure_retries=5,
    model=None,
    model_info=None,
    chunk_length=DEFAULT_CHUNK_LENGTH,
    api_key=EASYCOMPLETION_API_KEY,
    debug=DEBUG,
    temperature=0.0,
):
    """
    Function for sending text and returning a text completion response.

    Parameters:
        text (str): Text to send to the model.
        model_failure_retries (int, optional): Number of retries if the request fails. Default is 5.
        model (str, optional): The model to use. Deprecated.
        chunk_length (int, optional): Maximum length of text chunk to process. Deprecated.
        model_info (List[Tuple[str, int]], optional): The list of models to use, and their respective chuck length. Default is the DEFAULT_MODEL_INFO defined in constants.py.
        api_key (str, optional): OpenAI API key. If not provided, it uses the one defined in constants.py.

    Returns:
        str: The response content from the model.

    Example:
        >>> text_completion("Hello, how are you?", model_failure_retries=3, model='gpt-3.5-turbo', chunk_length=1024, api_key='your_openai_api_key')
    """

    # Use the default model if no model is specified
    models, error = sanity_check(text, model_info=model_info, model=model, chunk_length=chunk_length, api_key=api_key, debug=debug)
    if error:
        return error

    # Prepare messages for the API call
    messages = [{"role": "user", "content": text}]

    # Try to make a request for a specified number of times
    response, error = do_chat_completion(
        messages, models, temperature=temperature, model_failure_retries=model_failure_retries, debug=debug)
    if error:
        return error

    # Extract content from the response
    text = response["choices"][0]["message"]["content"]
    finish_reason = response["choices"][0]["finish_reason"]
    usage = response["usage"]

    return {
        "text": text,
        "usage": usage,
        "finish_reason": finish_reason,
        "error": None,
    }

async def text_completion_async(
    text,
    model_failure_retries=5,
    model=None,
    model_info=None,
    chunk_length=DEFAULT_CHUNK_LENGTH,
    api_key=EASYCOMPLETION_API_KEY,
    debug=DEBUG,
    temperature=0.0,
):
    """
    Function for sending text and returning a text completion response.

    Parameters:
        text (str): Text to send to the model.
        model_failure_retries (int, optional): Number of retries if the request fails. Default is 5.
        model (str, optional): The model to use. Deprecated.
        chunk_length (int, optional): Maximum length of text chunk to process. Deprecated.
        model_info (List[Tuple[str, int]], optional): The list of models to use, and their respective chuck length. Default is the DEFAULT_MODEL_INFO defined in constants.py.
        api_key (str, optional): OpenAI API key. If not provided, it uses the one defined in constants.py.

    Returns:
        str: The response content from the model.

    Example:
        >>> text_completion("Hello, how are you?", model_failure_retries=3, model='gpt-3.5-turbo', chunk_length=1024, api_key='your_openai_api_key')
    """

    # Use the default model if no model is specified
    models, error = sanity_check(text, model_info=model_info, model=model, chunk_length=chunk_length, api_key=api_key, debug=debug)
    if error:
        return error

    # Prepare messages for the API call
    messages = [{"role": "user", "content": text}]

    # Try to make a request for a specified number of times
    response, error = await asyncio.to_thread(lambda: do_chat_completion(
        messages, models, temperature=temperature, model_failure_retries=model_failure_retries, debug=debug))

    if error:
        return error

    # Extract content from the response
    text = response["choices"][0]["message"]["content"]
    finish_reason = response["choices"][0]["finish_reason"]
    usage = response["usage"]

    return {
        "text": text,
        "usage": usage,
        "finish_reason": finish_reason,
        "error": None,
    }


def function_completion(
    text=None,
    messages=None,
    system_message=None,
    functions=None,
    model_failure_retries=5,
    function_call=None,
    function_failure_retries=10,
    chunk_length=DEFAULT_CHUNK_LENGTH,
    model=None,
    model_info=None,
    api_key=EASYCOMPLETION_API_KEY,
    debug=DEBUG,
    temperature=0.0,
):
    """
    Send text and a list of functions to the model and return optional text and a function call.
    The function call is validated against the functions array.
    The input text is sent to the chat model and is treated as a user message.

    Args:
        text (str): Text that will be sent as the user message to the model.
        functions (list[dict] | dict | None): List of functions or a single function dictionary to be sent to the model.
        model_failure_retries (int): Number of times to retry the request if it fails (default is 5).
        function_call (str | dict | None): 'auto' to let the model decide, or a function name or a dictionary containing the function name (default is "auto").
        function_failure_retries (int): Number of times to retry the request if the function call is invalid (default is 10).
        model (str, optional): The model to use. Deprecated.
        chunk_length (int, optional): Maximum length of text chunk to process. Deprecated.
        model_info (List[Tuple[str, int]], optional): The list of models to use, and their respective chuck length. Default is the DEFAULT_MODEL_INFO defined in constants.py.
        api_key (str | None): If you'd like to pass in a key to override the environment variable EASYCOMPLETION_API_KEY.

    Returns:
        dict: On most errors, returns a dictionary with an "error" key. On success, returns a dictionary containing
        "text" (response from the model), "function_name" (name of the function called), "arguments" (arguments for the function), "error" (None).

    Example:
        >>> function = {'name': 'function1', 'parameters': {'param1': 'value1'}}
        >>> function_completion("Call the function.", function)
    """

    # Ensure that functions are provided
    if functions is None:
        return {"error": "functions is required"}

    # Check if a list of functions is provided
    if not isinstance(functions, list):
        if (
            isinstance(functions, dict)
            and "name" in functions
            and "parameters" in functions
        ):
            # A single function is provided as a dictionary, convert it to a list
            functions = [functions]
        else:
            # Functions must be either a list of dictionaries or a single dictionary
            return {
                "error": "functions must be a list of functions or a single function"
            }

    # Set the function call to the name of the function if only one function is provided
    # If there are multiple functions, use "auto"
    if function_call is None:
        function_call = functions[0]["name"] if len(functions) == 1 else "auto"

    # Make sure text is provided
    if text is None:
        log("Text is required", type="error", log=debug)
        return {"error": "text is required"}

    function_call_names = [function["name"] for function in functions]
    # check that all function_call_names are unique and in the text
    if len(function_call_names) != len(set(function_call_names)):
        log("Function names must be unique", type="error", log=debug)
        return {"error": "Function names must be unique"}

    if len(function_call_names) > 1 and not any(
        function_call_name in text for function_call_name in function_call_names
    ):
        log(
            "WARNING: Function and argument names should be in the text",
            type="warning",
            log=debug,
        )

    # Check if the function call is valid
    if function_call != "auto":
        if isinstance(function_call, str):
            function_call = {"name": function_call}
        elif "name" not in function_call:
            log("function_call must have a name property", type="error", log=debug)
            return {
                "error": "function_call had an invalid name. Should be a string of the function name or an object with a name property"
            }

    models, error = sanity_check(dict(
        text=text, functions=functions, messages=messages, system_message=system_message
        ), model_info=model_info, model=model, chunk_length=chunk_length, api_key=api_key)
    if error:
        return error

    # Count the number of tokens in the message
    message_tokens = count_tokens(text, model=models[0])
    total_tokens = message_tokens

    function_call_tokens = count_tokens(functions, model=models[0])
    total_tokens += function_call_tokens + 3  # Additional tokens for the user

    all_messages = []

    if system_message is not None:
        all_messages.append({"role": "system", "content": system_message})

    if messages is not None:
        all_messages += messages

    # Prepare the messages to be sent to the API
    if text is not None and text != "":
        all_messages.append({"role": "user", "content": text})

    # Retry function call and model calls according to the specified retry counts
    response = None
    for _ in range(function_failure_retries):
        # Try to make a request for a specified number of times
        response, error = do_chat_completion(
            all_messages, models, temperature=temperature, function_call=function_call,
            functions=functions, model_failure_retries=model_failure_retries, debug=debug)
        if error:
            time.sleep(1)
            continue
        print('***** response')
        print(response)
        if validate_functions(response, functions, function_call):
            break
        if response.get("choices", [{}])[0].get("finish_reason", None) == 'length':
            return {
                "text": None,
                "usage": response.usage,
                "finish_reason": 'length',
                "error": "Message too long",
            }
        time.sleep(1)

    # Check if we have a valid response from the model
    if not response:
        return error

    # Extracting the content and function call response from API response
    response_data = response["choices"][0]["message"]
    finish_reason = response["choices"][0]["finish_reason"]
    usage = response["usage"]

    text = response_data["content"]
    function_call_response = response_data.get("function_call", None)

    # If no function call in response, return an error
    if function_call_response is None:
        log(f"No function call in response\n{response}", type="error", log=debug)
        return {"error": "No function call in response"}
    function_name = function_call_response["name"]
    arguments = parse_arguments(function_call_response["arguments"])
    log(
        f"Response\n\nFunction Name: {function_name}\n\nArguments:\n{arguments}\n\nText:\n{text}\n\nFinish Reason: {finish_reason}\n\nUsage:\n{usage}",
        type="response",
        log=debug,
    )
    # Return the final result with the text response, function name, arguments and no error
    return {
        "text": text,
        "function_name": function_name,
        "arguments": arguments,
        "usage": usage,
        "finish_reason": finish_reason,
        "error": None,
    }

async def function_completion_async(
    text=None,
    messages=None,
    system_message=None,
    functions=None,
    model_failure_retries=5,
    function_call=None,
    function_failure_retries=10,
    chunk_length=DEFAULT_CHUNK_LENGTH,
    model=None,
    model_info=None,
    api_key=EASYCOMPLETION_API_KEY,
    debug=DEBUG,
    temperature=0.0,
):
    """
    Send text and a list of functions to the model and return optional text and a function call.
    The function call is validated against the functions array.
    The input text is sent to the chat model and is treated as a user message.

    Args:
        text (str): Text that will be sent as the user message to the model.
        functions (list[dict] | dict | None): List of functions or a single function dictionary to be sent to the model.
        model_failure_retries (int): Number of times to retry the request if it fails (default is 5).
        function_call (str | dict | None): 'auto' to let the model decide, or a function name or a dictionary containing the function name (default is "auto").
        function_failure_retries (int): Number of times to retry the request if the function call is invalid (default is 10).
        model (str, optional): The model to use. Deprecated.
        chunk_length (int, optional): Maximum length of text chunk to process. Deprecated.
        model_info (List[Tuple[str, int]], optional): The list of models to use, and their respective chuck length. Default is the DEFAULT_MODEL_INFO defined in constants.py.
        api_key (str | None): If you'd like to pass in a key to override the environment variable EASYCOMPLETION_API_KEY.

    Returns:
        dict: On most errors, returns a dictionary with an "error" key. On success, returns a dictionary containing
        "text" (response from the model), "function_name" (name of the function called), "arguments" (arguments for the function), "error" (None).

    Example:
        >>> function = {'name': 'function1', 'parameters': {'param1': 'value1'}}
        >>> function_completion("Call the function.", function)
    """

    # Ensure that functions are provided
    if functions is None:
        return {"error": "functions is required"}

    # Check if a list of functions is provided
    if not isinstance(functions, list):
        if (
            isinstance(functions, dict)
            and "name" in functions
            and "parameters" in functions
        ):
            # A single function is provided as a dictionary, convert it to a list
            functions = [functions]
        else:
            # Functions must be either a list of dictionaries or a single dictionary
            return {
                "error": "functions must be a list of functions or a single function"
            }

    # Set the function call to the name of the function if only one function is provided
    # If there are multiple functions, use "auto"
    if function_call is None:
        function_call = functions[0]["name"] if len(functions) == 1 else "auto"

    # Make sure text is provided
    if text is None:
        log("Text is required", type="error", log=debug)
        return {"error": "text is required"}

    function_call_names = [function["name"] for function in functions]
    # check that all function_call_names are unique and in the text
    if len(function_call_names) != len(set(function_call_names)):
        log("Function names must be unique", type="error", log=debug)
        return {"error": "Function names must be unique"}

    if len(function_call_names) > 1 and not any(
        function_call_name in text for function_call_name in function_call_names
    ):
        log(
            "WARNING: Function and argument names should be in the text",
            type="warning",
            log=debug,
        )

    # Check if the function call is valid
    if function_call != "auto":
        if isinstance(function_call, str):
            function_call = {"name": function_call}
        elif "name" not in function_call:
            log("function_call must have a name property", type="error", log=debug)
            return {
                "error": "function_call had an invalid name. Should be a string of the function name or an object with a name property"
            }

    models, error = sanity_check(dict(
        text=text, functions=functions, messages=messages, system_message=system_message
        ), model_info=model_info, model=model, chunk_length=chunk_length, api_key=api_key)
    if error:
        return error

    # Count the number of tokens in the message
    message_tokens = count_tokens(text, model=models[0])
    total_tokens = message_tokens

    function_call_tokens = count_tokens(functions, model=models[0])
    total_tokens += function_call_tokens + 3  # Additional tokens for the user

    all_messages = []

    if system_message is not None:
        all_messages.append({"role": "system", "content": system_message})

    if messages is not None:
        all_messages += messages

    # Prepare the messages to be sent to the API
    if text is not None and text != "":
        all_messages.append({"role": "user", "content": text})

    # Retry function call and model calls according to the specified retry counts
    response = None
    for _ in range(function_failure_retries):
        # Try to make a request for a specified number of times
        response, error = await asyncio.to_thread(lambda: do_chat_completion(
            all_messages, models, temperature=temperature, function_call=function_call,
            functions=functions, model_failure_retries=model_failure_retries, debug=debug))
        if error:
            time.sleep(1)
            continue
        print('***** response')
        print(response)
        if validate_functions(response, functions, function_call):
            break
        if response.get("choices", [{}])[0].get("finish_reason", None) == 'length':
            return {
                "text": None,
                "usage": response.usage,
                "finish_reason": 'length',
                "error": "Message too long",
            }
        time.sleep(1)

    # Check if we have a valid response from the model
    if not response:
        return error

    # Extracting the content and function call response from API response
    response_data = response["choices"][0]["message"]
    finish_reason = response["choices"][0]["finish_reason"]
    usage = response["usage"]

    text = response_data["content"]
    function_call_response = response_data.get("function_call", None)

    # If no function call in response, return an error
    if function_call_response is None:
        log(f"No function call in response\n{response}", type="error", log=debug)
        return {"error": "No function call in response"}
    function_name = function_call_response["name"]
    arguments = parse_arguments(function_call_response["arguments"])
    log(
        f"Response\n\nFunction Name: {function_name}\n\nArguments:\n{arguments}\n\nText:\n{text}\n\nFinish Reason: {finish_reason}\n\nUsage:\n{usage}",
        type="response",
        log=debug,
    )
    # Return the final result with the text response, function name, arguments and no error
    return {
        "text": text,
        "function_name": function_name,
        "arguments": arguments,
        "usage": usage,
        "finish_reason": finish_reason,
        "error": None,
    }
