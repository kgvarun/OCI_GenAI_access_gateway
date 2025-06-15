#!/bin/sh
curl -N http://localhost:${OCI_GW_PORT}/api/v1/chat/completions   -H "Authorization: Bearer ${DEFAULT_API_KEYS}"   -H "Content-Type: application/json"   -d '{
    "model": "openai.o4-mini",
    "messages": [
      { "role": "system", "content": "You are a helpful assistant." },
      { "role": "user",   "content": "Hello, how are you today?" }
    ],
    "stream": true
  }'

exit

curl -N http://localhost:${OCI_GW_PORT}/api/v1/chat/completions   -H "Authorization: Bearer ${DEFAULT_API_KEYS}"   -H "Content-Type: application/json"   -d '{
    "model": "meta.llama-4-maverick-17b-128e-instruct-fp8",
    "messages": [
      { "role": "system", "content": "You are a helpful assistant." },
      { "role": "user",   "content": "Hello, how are you today?" }
    ],
    "stream": true
  }'



