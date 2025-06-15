import os
PORT = os.getenv("OCI_GW_PORT")
RELOAD = os.getenv("OCI_GW_RELOAD")
DEBUG = os.getenv("OCI_GW_DEBUG")
DEFAULT_API_KEYS = os.getenv("DEFAULT_API_KEYS")
API_ROUTE_PREFIX = "/api/v1"

EMBED_TRUNCATE = "END"
# One of NONE|START|END to specify how the API will handle inputs longer than the maximum token length.
# START: will discard the start of the input until the remaining input is exactly the maximum input token length for the model. 
# END: will discard the end of the input until the remaining input is exactly the maximum input token length for the model.
# NONE: when the input exceeds the maximum input token length an error will be returned


# AUTH_TYPE can be "API_KEY" or "INSTANCE_PRINCIPAL"
AUTH_TYPE = os.getenv("AUTH_TYPE")
OCI_CONFIG_FILE= os.getenv("OCI_CONFIG_FILE")
OCI_CONFIG_FILE_KEY = os.getenv("OCI_CONFIG_FILE_KEY")
INFERENCE_ENDPOINT_TEMPLATE = "https://inference.generativeai.{region}.oci.oraclecloud.com/20231130"

TITLE = "OCI Generative AI Proxy APIs"
SUMMARY = "OpenAI-Compatible RESTful APIs for OCI Generative AI Service"
VERSION = "0.1.0"
DESCRIPTION = """
Use OpenAI-Compatible RESTful APIs for OCI Generative AI Service models and OCI Data Science AI quick actions models.

Please edit "models.yaml" to specify your models and their call endpoints.
"""