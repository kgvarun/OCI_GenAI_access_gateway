import base64
import json
import logging
import re
import time
from abc import ABC
from typing import AsyncIterable, Iterable, Literal
from collections.abc import Iterable

import oci
from oci.generative_ai_inference import models as oci_models
from api.setting import DEBUG
from api.setting import CLIENT_KWARGS, \
    INFERENCE_ENDPOINT_TEMPLATE, \
    SUPPORTED_OCIGENAI_EMBEDDING_MODELS, \
    SUPPORTED_OCIGENAI_CHAT_MODELS

import numpy as np
import requests
import tiktoken
from fastapi import HTTPException

from api.models.base import BaseChatModel, BaseEmbeddingsModel
from api.schema import (
    # Chat
    ChatResponse,
    ChatRequest,
    Choice,
    ChatResponseMessage,
    Usage,
    ChatStreamResponse,
    ImageContent,
    TextContent,
    ToolCall,
    ChoiceDelta,
    UserMessage,
    AssistantMessage,
    ToolMessage,
    Function,
    ResponseFunction,
    # Embeddings
    EmbeddingsRequest,
    EmbeddingsResponse,
    EmbeddingsUsage,
    Embedding,
    Convertor
)
from config import EMBED_TRUNCATE

logging.basicConfig(format='%(asctime)s,%(msecs)03d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s',
                    datefmt='%Y-%m-%d:%H:%M:%S',
                    level=logging.DEBUG)
logger = logging.getLogger(__name__)

generative_ai_inference_client = oci.generative_ai_inference.GenerativeAiInferenceClient(
    **CLIENT_KWARGS
)

ENCODER = tiktoken.get_encoding("cl100k_base")


class OCIGenAIModel(BaseChatModel):
    # https://docs.oracle.com/en-us/iaas/Content/generative-ai/pretrained-models.htm
    # https://docs.oracle.com/en-us/iaas/data-science/using/ai-quick-actions-model-deploy.htm
    _supported_models = {}

    for model in SUPPORTED_OCIGENAI_CHAT_MODELS:
        model_setting = SUPPORTED_OCIGENAI_CHAT_MODELS[model]
        _supported_models[model] = {
            "system": model_setting.get('system', True),
            "multimodal": model_setting.get('multimodal', False),
            "tool_call": model_setting.get('tool_call', False),
            "stream_tool_call": model_setting.get('stream_tool_call', False),
        }

    def list_models(self) -> list[str]:
        return list(self._supported_models.keys())

    def validate(self, chat_request: ChatRequest):
        """Perform basic validation on requests"""
        error = ""
        # check if model is supported
        if chat_request.model not in self._supported_models.keys():
            error = f"Unsupported model {chat_request.model}, please use models API to get a list of supported models"

        # check if tool call is supported
        elif chat_request.tools and not self._is_tool_call_supported(chat_request.model, stream=chat_request.stream):
            tool_call_info = "Tool call with streaming" if chat_request.stream else "Tool call"
            error = f"{tool_call_info} is currently not supported by {chat_request.model}"

        if error:
            raise HTTPException(
                status_code=400,
                detail=error,
            )

    def _invoke_genai(self, chat_request: ChatRequest, stream=False):
        """Common logic for invoke OCI GenAI models"""
        if DEBUG:
            logger.info("Raw request:\n" + chat_request.model_dump_json())

        # convert OpenAI chat request to OCI Generative AI SDK request
        chat_detail = self._parse_request(chat_request)
        if DEBUG:
            logger.info("OCI Generative AI request:\n" + json.dumps(json.loads(str(chat_detail)), ensure_ascii=False))
        try:
            region = SUPPORTED_OCIGENAI_CHAT_MODELS[chat_request.model]["region"]
            # generative_ai_inference_client.base_client.config["region"] = region
            generative_ai_inference_client.base_client._endpoint = INFERENCE_ENDPOINT_TEMPLATE.replace("{region}", region)
            response = generative_ai_inference_client.chat(chat_detail)
            if DEBUG and not chat_detail.chat_request.is_stream:
                logger.info("OCI Generative AI response:\n" + json.dumps(json.loads(str(response.data)), ensure_ascii=False))
        except Exception as e:
            logger.error(e)
            raise HTTPException(status_code=500, detail=str(e))
        return response

    def chat(self, chat_request: ChatRequest) -> ChatResponse:
        """Default implementation for Chat API."""

        # message_id = self.generate_message_id()
        response = self._invoke_genai(chat_request)
        message_id = response.request_id

        chat_response = self._create_response(
            model=response.data.model_id,
            message_id=message_id,
            chat_response=response.data.chat_response,
            input_tokens=0,
            output_tokens=0,
        )
        if DEBUG:
            logger.info("Proxy response :" + chat_response.model_dump_json())
        return chat_response

    def chat_stream(self, chat_request: ChatRequest) -> AsyncIterable[bytes]:
        """Default implementation for Chat Stream API"""
        response = self._invoke_genai(chat_request)
        if not response.data:
            raise HTTPException(status_code=500, detail="OCI AI API returned empty response")

        message_id = response.request_id
        model_id = SUPPORTED_OCIGENAI_CHAT_MODELS[chat_request.model]["model_id"]
        provider = SUPPORTED_OCIGENAI_CHAT_MODELS[chat_request.model]["provider"]
        
        events = response.data.events()
        for stream in events:
            try:
                chunk = json.loads(stream.data)
                if DEBUG:
                    logger.info("Raw chunk: " + str(chunk))
                
                stream_response = self._create_response_stream(
                    model_id=model_id, message_id=message_id, chunk=chunk
                )
                if not stream_response:
                    continue
                if DEBUG:
                    logger.info("Proxy response :" + stream_response.model_dump_json())
                if stream_response.choices:
                    yield self.stream_response_to_bytes(stream_response)
                elif (
                        chat_request.stream_options
                        and chat_request.stream_options.include_usage
                ):
                    yield self.stream_response_to_bytes(stream_response)
            except Exception as e:
                logger.error(f"Error processing stream chunk: {e}")
                continue

        # return an [DONE] message at the end.
        yield self.stream_response_to_bytes()

    def _parse_system_prompts(self, chat_request: ChatRequest) -> list[dict[str, str]]:
        """Create system prompts.
        Note that not all models support system prompts.

        example output: [{"text" : system_prompt}]

        See example:
        https://docs.oracle.com/en-us/iaas/api/#/EN/generative-ai-inference/20231130/ChatResult/Chat
        """

        system_prompts = []
        for message in chat_request.messages:
            if message.role != "system":
                # ignore system messages here
                continue
            assert isinstance(message.content, str)
            system_prompts.append(message.content)

        return system_prompts

    def _parse_messages(self, chat_request: ChatRequest) -> list[dict]:
        """
        Converse API only support user and assistant messages.

        example output: [{
            "role": "user",
            "content": [{"text": input_text}]
        }]

        See example:
        https://docs.oracle.com/en-us/iaas/api/#/EN/generative-ai-inference/20231130/ChatResult/Chat
        """
        messages = []
        for message in chat_request.messages:
            if isinstance(message, UserMessage):
                messages.append(
                    {
                        "role": message.role,
                        "content": self._parse_content_parts(
                            message, chat_request.model
                        ),
                    }
                )
            elif isinstance(message, AssistantMessage):
                if message.content:
                    # Text message
                    messages.append(
                        {"role": message.role, "content": [{"text": message.content}]}
                    )
                elif message.tool_calls:
                    # Tool use message
                    # formate https://platform.openai.com/docs/guides/function-calling?api-mode=chat#handling-function-calls                    
                    messages.append({"role": message.role,"tool_calls": message.tool_calls})
            elif isinstance(message, ToolMessage):
                # Add toolResult to content
                # https://docs.oracle.com/en-us/iaas/api/#/EN/generative-ai-inference/20231130/ChatResult/Chat
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content
                    }
                )

            else:
                # ignore others, such as system messages
                continue
        return messages

    def _parse_request(self, chat_request: ChatRequest) -> oci_models.ChatDetails:
        """Create default converse request body.

        Also perform validations to tool call etc.

        Ref: https://docs.oracle.com/en-us/iaas/api/#/EN/generative-ai-inference/20231130/ChatResult/Chat
        """

        messages = self._parse_messages(chat_request)
        system_prompts = self._parse_system_prompts(chat_request)
        

        # Base inference parameters - start with common parameters
        inference_config = {
            "is_stream": chat_request.stream,
            "frequency_penalty": chat_request.frequency_penalty,
            "presence_penalty": chat_request.presence_penalty,
            "temperature": chat_request.temperature,
            "top_p": chat_request.top_p
        }

        # Add max tokens based on provider
        model_name = chat_request.model
        provider = SUPPORTED_OCIGENAI_CHAT_MODELS[model_name]["provider"]
        if provider == "openai":
            # Don't include max_completion_tokens in initial config
            pass
        else:
            inference_config["max_tokens"] = chat_request.max_tokens

        compartment_id = SUPPORTED_OCIGENAI_CHAT_MODELS[model_name]["compartment_id"]

        if provider == "dedicated":
            endpoint = SUPPORTED_OCIGENAI_CHAT_MODELS[model_name]["endpoint"]
            servingMode = oci_models.DedicatedServingMode(
                serving_type = "DEDICATED",
                endpoint_id = endpoint
                )
        else:
            model_id = SUPPORTED_OCIGENAI_CHAT_MODELS[model_name]["model_id"]
            servingMode = oci_models.OnDemandServingMode(
                serving_type = "ON_DEMAND",
                model_id = model_id
                )
        chat_detail = oci_models.ChatDetails(
            compartment_id = compartment_id,
            serving_mode = servingMode,
            # chat_request = chatRequest
            )  
        
        if provider == "cohere":
            cohere_chatRequest = oci_models.CohereChatRequest(**inference_config)
            if system_prompts:
                cohere_chatRequest.preamble_override = ' '.join(system_prompts)
            
            # add tools
            if chat_request.tools:
                cohere_tools = Convertor.convert_tools_openai_to_cohere(chat_request.tools)
                cohere_chatRequest.tools = cohere_tools  
            
            chatHistory = []
            for i,message in enumerate(messages):
                # process chat history
                if i < len(messages)-1:                
                    # print("="*22,'\n',message)
                    # text = text.encode("unicode_escape").decode("utf-8")
                    try:
                        text = message["content"][0]["text"]
                    except:
                        text = ""               
                    if message["role"] == "user":
                        message_line = oci_models.CohereUserMessage(
                            role = "USER",
                            message = text
                            )             
                    elif message["role"] == "assistant":
                        if "tool_calls" in message:
                            if not message["tool_calls"]:
                                message_line = oci_models.CohereChatBotMessage(
                                    role = "CHATBOT",
                                    message = text
                                    )
                            else:
                                message_line = oci_models.CohereChatBotMessage(
                                    role = "CHATBOT",
                                    message = text,
                                    tool_calls = Convertor.convert_tool_calls_openai_to_cohere(message["tool_calls"])
                                    ) 
                        else:
                            message_line = oci_models.CohereChatBotMessage(
                                    role = "CHATBOT",
                                    message = text
                                    )                 

                    elif message["role"] == "tool":
                        cohere_tool_results = []
                        cohere_tool_result = Convertor.convert_tool_result_openai_to_cohere(message)
                        cohere_tool_results.append(cohere_tool_result)
                        message_line = oci_models.CohereToolMessage(
                            role = "TOOL",
                            tool_results = cohere_tool_results
                            )
                        
                    chatHistory.append(message_line)
                # process the last message    
                elif i == len(messages)-1:
                    if message["role"] in ("user","assistant","system"):
                        cohere_chatRequest.message = message["content"][0]["text"]
                        # text = text.encode("unicode_escape").decode("utf-8")
                    # input tool result
                    elif message["role"] == "tool":
                        cohere_chatRequest.message = ""
                        cohere_tool_results = []
                        cohere_tool_result = Convertor.convert_tool_result_openai_to_cohere(message)
                        cohere_tool_results.append(cohere_tool_result)
                        cohere_chatRequest.tool_results = cohere_tool_results

                cohere_chatRequest.chat_history = chatHistory
            chat_detail.chat_request = cohere_chatRequest

        elif provider == "meta":
            generic_chatRequest = oci_models.GenericChatRequest(**inference_config)
            generic_chatRequest.numGenerations = chat_request.n
            generic_chatRequest.topK = -1
            
            # add tools
            if chat_request.tools:
                llama_tools = Convertor.convert_tools_openai_to_llama(chat_request.tools)
                generic_chatRequest.tools = llama_tools

            meta_messages = []
            for message in messages:
                message["role"] = message["role"].upper()
                if message["role"] == "SYSTEM":
                    meta_message = oci_models.SystemMessage(
                        role = "SYSTEM",
                        content = [oci_models.TextContent(type = "TEXT",text = c["text"]) for c in message["content"]]
                    )

                elif message["role"] == "USER":
                    content = []
                    for c in message["content"]:
                        if c["type"] == "TEXT":
                            content.append(oci_models.TextContent(type = "TEXT",text = c["text"]))
                        elif c["type"] == "IMAGE":
                            content.append(oci_models.ImageContent(type = "IMAGE",image_url  = c["imageUrl"]))                        

                    meta_message = oci_models.UserMessage(
                        role = "USER",
                        content = content
                        )
                
                elif message["role"] == "TOOL":
                    meta_message = Convertor.convert_tool_result_openai_to_llama(message)

                elif message["role"] == "ASSISTANT":
                    content = None
                    tool_calls = None
                    if "content" in message:
                        if message["content"]:
                            content = [oci_models.TextContent(type = "TEXT",text = c["text"]) for c in message["content"]]
                    
                    if "tool_calls" in message:
                        if message["tool_calls"]:
                            tool_calls = Convertor.convert_tool_calls_openai_to_llama(message["tool_calls"])

                    meta_message = oci_models.AssistantMessage(
                        role = "ASSISTANT",
                        content = content,
                        tool_calls = tool_calls
                        )
                else:
                    meta_message = message

                meta_messages.append(meta_message)
            generic_chatRequest.messages = meta_messages
            chat_detail.chat_request = generic_chatRequest

        # ---------------------------------------------------------------------
        # OpenAI‐style chat models (hosted on OCI GenAI)
        # ---------------------------------------------------------------------
        elif provider == "openai":
            # Create OpenAI-style chat request
            generic_chatRequest = oci_models.GenericChatRequest(**inference_config)
            generic_chatRequest.api_format = oci_models.BaseChatRequest.API_FORMAT_GENERIC
            generic_chatRequest.max_completion_tokens = chat_request.max_tokens

            # Handle system messages
            if system_prompts:
                system_message = oci_models.Message()
                system_message.role = "SYSTEM"
                system_message.content = [
                    oci_models.TextContent(text=prompt)
                    for prompt in system_prompts
                ]
                oci_messages = [system_message]
            else:
                oci_messages = []

            # Convert messages to OCI format
            for msg in messages:
                oci_msg = oci_models.Message()
                oci_msg.role = msg["role"].upper()
                
                if "tool_calls" in msg:
                    # Handle tool calls
                    oci_msg.tool_calls = Convertor.convert_tool_calls_openai_to_oci_openai(msg["tool_calls"])
                    oci_msg.content = []  # Tool calls don't have content
                else:
                    # Handle regular content
                    oci_msg.content = [
                        oci_models.TextContent(text=part["text"])
                        for part in msg["content"]
                    ]
                oci_messages.append(oci_msg)

            # Attach function definitions if present
            if chat_request.tools:
                generic_chatRequest.tools = Convertor.convert_tools_openai_to_oci_openai(chat_request.tools)

            generic_chatRequest.messages = oci_messages
            chat_detail.chat_request = generic_chatRequest

        return chat_detail

    def _create_response(
            self,
            model: str,
            message_id: str,
            chat_response = None,
            input_tokens: int = 0,
            output_tokens: int = 0,
    ) -> ChatResponse:
        message = ChatResponseMessage(role="assistant")
        
        if type(chat_response) == oci_models.CohereChatResponse:
            finish_reason = chat_response.finish_reason
            if chat_response.tool_calls:
                message.tool_calls = Convertor.convert_tool_calls_cohere_to_openai(chat_response.tool_calls)
                message.content = None
            else:
                message.content = chat_response.text
        elif type(chat_response) == oci_models.GenericChatResponse:
            choice = chat_response.choices[0]
            finish_reason = choice.finish_reason
            
            if choice.message.tool_calls:
                # Handle tool calls
                message.tool_calls = Convertor.convert_tool_calls_oci_openai_to_openai(choice.message.tool_calls)
                message.content = None
            elif choice.message.content:
                # Handle regular content
                message.content = choice.message.content[0].text
            else:
                message.content = None

        response = ChatResponse(
            id = message_id,
            model = model,
            choices = [
                Choice(
                    index=0,
                    message=message,
                    finish_reason=self._convert_finish_reason(finish_reason),
                    logprobs=None,
                )
            ],
            usage=Usage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )
        response.system_fingerprint = "fp"
        response.object = "chat.completion"
        response.created = int(time.time())
        return response

    def _create_response_stream(
            self, model_id: str, message_id: str, chunk: dict
    ) -> ChatStreamResponse | None:
        """Parsing the OCI GenAI stream response chunk.

        Ref: https://docs.oracle.com/en-us/iaas/api/#/EN/generative-ai-inference/20231130/ChatResult/Chat
        """
        if DEBUG:
            logger.info("OCI GenAI response chunk: " + str(chunk))
            
        finish_reason = None
        message = None
        usage = None

        if "finishReason" not in chunk:
            if "choices" in chunk:
                choice = chunk["choices"][0]
                delta = choice.get("delta", {})
                
                if "tool_calls" in delta:
                    # Handle tool calls in streaming
                    tool_calls = Convertor.convert_tool_calls_oci_openai_to_openai(delta["tool_calls"])
                    message = ChatResponseMessage(
                        role="assistant",
                        tool_calls=tool_calls
                    )
                elif "content" in delta:
                    # Handle regular content in streaming
                    message = ChatResponseMessage(
                        role="assistant",
                        content=delta["content"]
                    )
                elif "role" in delta:
                    # Handle role-only delta (initial message)
                    message = ChatResponseMessage(role="assistant")
                elif "message" in choice:
                    # Handle OpenAI-style message content
                    if isinstance(choice["message"], dict):
                        content_list = choice["message"].get("content", [])
                        if content_list and isinstance(content_list, list):
                            content = content_list[0].get("text", "")
                            if content:
                                message = ChatResponseMessage(
                                    role="assistant",
                                    content=content
                                )
            elif "message" in chunk:
                # Handle direct message in chunk (OpenAI format)
                if isinstance(chunk["message"], dict):
                    content_list = chunk["message"].get("content", [])
                    if content_list and isinstance(content_list, list):
                        content = content_list[0].get("text", "")
                        if content:
                            message = ChatResponseMessage(
                                role="assistant",
                                content=content
                            )
            elif model_id.startswith("cohere"):
                if "toolCalls" in chunk:
                    tool_calls = Convertor.convert_tool_calls_cohere_to_openai(chunk["toolCalls"])
                    message = ChatResponseMessage(
                        role="assistant",
                        tool_calls=tool_calls
                    )
                else:
                    message = ChatResponseMessage(
                        role="assistant",
                        content=chunk.get("text", "")
                    )
                    
            elif model_id.startswith("meta"):
                if "toolCalls" in chunk["message"]:
                    tool_calls = Convertor.convert_tool_calls_llama_to_openai(chunk["message"]["toolCalls"])
                    message = ChatResponseMessage(
                        role="assistant",
                        tool_calls=tool_calls
                    )
                else:
                    content = chunk["message"].get("content", [{}])[0].get("text", "")
                    message = ChatResponseMessage(
                        role="assistant",
                        content=content
                    )
                    
        elif "finishReason" in chunk:
            message = ChatResponseMessage(role="assistant")
            finish_reason = chunk["finishReason"]
            
            if "toolCalls" in chunk:
                tool_calls = Convertor.convert_tool_calls_cohere_to_openai(chunk["toolCalls"])
                message.tool_calls = tool_calls
                message.content = None

        if "metadata" in chunk:
            # usage information in metadata.
            metadata = chunk["metadata"]
            if "usage" in metadata:
                # token usage
                return ChatStreamResponse(
                    id=message_id,
                    model=model_id,
                    choices=[],
                    usage=Usage(
                        prompt_tokens=metadata["usage"]["inputTokens"],
                        completion_tokens=metadata["usage"]["outputTokens"],
                        total_tokens=metadata["usage"]["totalTokens"],
                    ),
                )
        if message:
            return ChatStreamResponse(
                id=message_id,
                model=model_id,
                choices=[
                    ChoiceDelta(
                        index=0,
                        delta=message,
                        logprobs=None,
                        finish_reason=self._convert_finish_reason(finish_reason),
                    )
                ],
                usage=usage,
            )

        return None

    def _parse_content_parts(
            self,
            message: UserMessage,
            model_id: str,
    ) -> list[dict]:
        if isinstance(message.content, str):
            return [
                {
                    "type": "TEXT",
                    "text": message.content,
                }
            ]
        content_parts = []
        for part in message.content:
            if isinstance(part, TextContent):
                content_parts.append(
                    {
                        "type": "TEXT",
                        "text": part.text,
                    }
                )
            elif isinstance(part, ImageContent):
                if not self._is_multimodal_supported(model_id):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Multimodal message is currently not supported by {model_id}",
                    )
                # image_data, content_type = self._parse_image(part.image_url.url)
                content_parts.append(                    
                    {
                        "type": "IMAGE",
                        "imageUrl": {"url": f"{part.image_url.url}"},
                    }
                )
            else:
                # Ignore..
                continue
        return content_parts

    def _is_tool_call_supported(self, model_id: str, stream: bool = False) -> bool:
        feature = self._supported_models.get(model_id)
        if not feature:
            return False
        return feature["stream_tool_call"] if stream else feature["tool_call"]

    def _is_multimodal_supported(self, model_id: str) -> bool:
        feature = self._supported_models.get(model_id)
        if not feature:
            return False
        return feature["multimodal"]

    def _is_system_prompt_supported(self, model_id: str) -> bool:
        feature = self._supported_models.get(model_id)
        if not feature:
            return False
        return feature["system"]

    # def _convert_tool_spec(self, func: Function) -> dict:

    #     return {
    #             "name": func.name,
    #             "description": func.description,
    #             "parameter_definitions": {
    #                 "type":
    #                 "description":
    #                 "is_required":
    #                 "json": func.parameters,
    #             }
    #         }

    def _convert_finish_reason(self, finish_reason: str | None) -> str | None:
        """
        Below is a list of finish reason according to OpenAI doc:

        - stop: if the model hit a natural stop point or a provided stop sequence,
        - length: if the maximum number of tokens specified in the request was reached,
        - content_filter: if content was omitted due to a flag from our content filters,
        - tool_calls: if the model called a tool
        """
        if finish_reason:
            finish_reason_mapping = {
                "tool_use": "tool_calls",
                "COMPLETE": "stop",
                "ERROR_TOXIC": "content_filter",
                "ERROR_LIMIT": "stop",
                "ERROR": "stop",
                "USER_CANCEL": "stop",
                "MAX_TOKENS": "length",
            }
            return finish_reason_mapping.get(finish_reason.lower(), finish_reason.lower())
        return None


class OCIGenAIEmbeddingsModel(BaseEmbeddingsModel, ABC):
    accept = "application/json"
    content_type = "application/json"

    def _invoke_model(self, args: dict, model_id: str):
        # body = json.dumps(args)
        compartment_id = SUPPORTED_OCIGENAI_EMBEDDING_MODELS[model_id]["compartment_id"]
        region = SUPPORTED_OCIGENAI_EMBEDDING_MODELS[model_id]["region"]
        generative_ai_inference_client.base_client._endpoint = INFERENCE_ENDPOINT_TEMPLATE.replace("{region}", region)
        body = {
            "inputs": args["texts"],
            "servingMode": {"servingType": "ON_DEMAND", "modelId": model_id},
            "truncate": args["truncate"],
            "compartmentId": compartment_id
        }
        if DEBUG:
            logger.info("Invoke OCI GenAI Model: " + model_id)
            logger.info("OCI GenAI request body: " + json.dumps(body))
        try:

            embed_text_response = generative_ai_inference_client.embed_text(body)
            return embed_text_response

        except Exception as e:
            logger.error("Validation Error: " + str(e))
            raise HTTPException(status_code=400, detail=str(e))

    def _create_response(
            self,
            embeddings: list[float],
            model: str,
            input_tokens: int = 0,
            output_tokens: int = 0,
            encoding_format: Literal["float", "base64"] = "float",
    ) -> EmbeddingsResponse:
        data = []
        for i, embedding in enumerate(embeddings):
            if encoding_format == "base64":
                arr = np.array(embedding, dtype=np.float32)
                arr_bytes = arr.tobytes()
                encoded_embedding = base64.b64encode(arr_bytes)
                data.append(Embedding(index=i, embedding=encoded_embedding))
            else:
                data.append(Embedding(index=i, embedding=embedding))

        response = EmbeddingsResponse(
            data=data,
            model=model,
            usage=EmbeddingsUsage(
                prompt_tokens=input_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )
        if DEBUG:
            logger.info("Proxy response :" + response.model_dump_json()[:100])
        return response


class CohereEmbeddingsModel(OCIGenAIEmbeddingsModel):

    def _parse_args(self, embeddings_request: EmbeddingsRequest) -> dict:
        texts = []
        if isinstance(embeddings_request.input, str):
            texts = [embeddings_request.input]
        elif isinstance(embeddings_request.input, list):
            texts = embeddings_request.input
        elif isinstance(embeddings_request.input, Iterable):
            # For encoded input
            # The workaround is to use tiktoken to decode to get the original text.
            encodings = []
            for inner in embeddings_request.input:
                if isinstance(inner, int):
                    # Iterable[int]
                    encodings.append(inner)
                else:
                    # Iterable[Iterable[int]]
                    text = ENCODER.decode(list(inner))
                    texts.append(text)
            if encodings:
                texts.append(ENCODER.decode(encodings))

        # Maximum of 2048 characters
        args = {
            "texts": texts,
            "input_type": "search_document",
            "truncate": EMBED_TRUNCATE,  # "NONE|START|END"
        }
        return args

    def embed(self, embeddings_request: EmbeddingsRequest) -> EmbeddingsResponse:
        response = self._invoke_model(
            args=self._parse_args(embeddings_request), model_id=embeddings_request.model
        )
        response_body = response.data
        if DEBUG:
            logger.info("OCI GenAI response body: " + str(response_body)[:50])

        return self._create_response(
            embeddings=response_body.embeddings,
            model=response_body.model_id,
            encoding_format=embeddings_request.encoding_format,
        )


def get_embeddings_model(model_id: str) -> OCIGenAIEmbeddingsModel:
    model_name = SUPPORTED_OCIGENAI_EMBEDDING_MODELS.get(model_id, "")
    if model_name:
        if DEBUG:
            logger.info("model name is " + model_name["name"])
        return CohereEmbeddingsModel()
    else:
        logger.error("Unsupported model id " + model_id)
        raise HTTPException(
            status_code=400,
            detail="Unsupported embedding model id " + model_id,
        )
