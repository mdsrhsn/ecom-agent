"""
Gemini agentic loop — drop-in replacement for the old Anthropic Claude client.

We keep the SAME public function `chat(user_message, db, history=None)` so that
app/routes/api.py doesn't need any change.

Gemini's function-calling format differs from Anthropic's tools format, so we
convert our existing TOOL_SPECS (Anthropic-style) into Gemini-style declarations
at import time.
"""
import json
from sqlalchemy.orm import Session

import google.generativeai as genai
from google.generativeai.types import (
    FunctionDeclaration,
    Tool,
    HarmCategory,
    HarmBlockThreshold,
)

from app.config import settings
from app.agent.tools import TOOL_SPECS, run_tool


MODEL_NAME = "gemini-2.5-flash-lite"   # higher free tier (1000 RPD vs 20 RPD on flash)


SYSTEM_PROMPT = """You are an AI agent helping Mudassar manage his e-commerce business.
He runs a Shopify store and uses multiple Pakistani couriers (PostEx, Daewoo, DigiDokaan, Leopards, TCS).

Your job: give accurate, fast answers about orders, shipments, inventory, and payments.

Style:
- Mudassar communicates in Roman Urdu + English. Match his style — friendly, direct, business-like.
- Always give NUMBERS first, then context. He values precision.
- Use tools to fetch live data. Never make up counts or status — always call a tool first.
- For multi-part questions, call multiple tools in sequence.
- Money is in PKR.
- When listing critical or overdue items, include tracking number and customer phone — his team needs to call.

CRITICAL status meanings:
- return_in_process: parcel under return decision, MAY still deliver
- return_to_shipper: confirmed, parcel coming back to Mudassar
- received_back: he has it physically again
Treat these three as DISTINCT. Never lump them together.

Inventory rule:
pcs_pending = pcs_sent_to_courier - pcs_paid - pcs_return_to_shipper - pcs_received_back

If a tool returns an error (e.g. API key missing), tell Mudassar directly. Do not hide errors.
"""


# ---------------------------------------------------------------------------
# Convert our Anthropic-style TOOL_SPECS into Gemini FunctionDeclaration list.
# Anthropic uses {"name", "description", "input_schema"}.
# Gemini uses    {"name", "description", "parameters"}.
# ---------------------------------------------------------------------------
def _to_gemini_tool() -> Tool:
    declarations = []
    for spec in TOOL_SPECS:
        params = spec.get("input_schema") or {"type": "object", "properties": {}}
        # Gemini does not accept "default" inside parameter properties.
        clean_props = {}
        for prop_name, prop_def in (params.get("properties") or {}).items():
            clean = {k: v for k, v in prop_def.items() if k != "default"}
            clean_props[prop_name] = clean
        clean_params = {
            "type": params.get("type", "object"),
            "properties": clean_props,
        }
        if params.get("required"):
            clean_params["required"] = params["required"]

        declarations.append(
            FunctionDeclaration(
                name=spec["name"],
                description=spec["description"],
                parameters=clean_params,
            )
        )
    return Tool(function_declarations=declarations)


_GEMINI_TOOL = None
_MODEL = None


def _model():
    global _GEMINI_TOOL, _MODEL
    if _MODEL is None:
        api_key = settings.GEMINI_API_KEY
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY missing in .env — add your key from "
                "https://aistudio.google.com/apikey"
            )
        genai.configure(api_key=api_key)
        _GEMINI_TOOL = _to_gemini_tool()
        _MODEL = genai.GenerativeModel(
            model_name=MODEL_NAME,
            system_instruction=SYSTEM_PROMPT,
            tools=[_GEMINI_TOOL],
            # be permissive — this is a business tool, not consumer content
            safety_settings={
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            },
        )
    return _MODEL


def _history_to_gemini(history: list) -> list:
    """
    Convert our stored history (list of {role, content} dicts) into Gemini's
    chat history format. We only persist plain text user/assistant turns.
    """
    out = []
    for msg in history or []:
        role = msg.get("role")
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content
        else:
            # Already in some structured form — best-effort flatten to text.
            text = json.dumps(content, default=str)
        gem_role = "user" if role == "user" else "model"
        out.append({"role": gem_role, "parts": [{"text": text}]})
    return out


async def chat(user_message: str, db: Session, history: list = None) -> dict:
    """
    Public entrypoint matching the old Claude client signature.
    Returns: {"reply": "<text>", "tool_calls": [...]}
    """
    try:
        model = _model()
    except RuntimeError as e:
        return {"reply": f"⚠️ {e}", "tool_calls": []}

    chat_session = model.start_chat(history=_history_to_gemini(history))

    tool_calls_made = []
    current_input = user_message

    # agentic loop — up to 6 tool-use rounds
    for _ in range(6):
        try:
            response = chat_session.send_message(current_input)
        except Exception as e:
            return {
                "reply": f"⚠️ Gemini API error: {type(e).__name__}: {e}",
                "tool_calls": tool_calls_made,
            }

        # Look at the parts of the response. A part is either text or a function_call.
        function_calls = []
        text_parts = []
        try:
            for part in response.candidates[0].content.parts:
                # function_call attr is set when the model wants a tool
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    function_calls.append(fc)
                else:
                    txt = getattr(part, "text", None)
                    if txt:
                        text_parts.append(txt)
        except (IndexError, AttributeError):
            # Fall back to the SDK's convenience accessor
            txt = getattr(response, "text", "") or ""
            return {"reply": txt.strip() or "(empty reply)", "tool_calls": tool_calls_made}

        if not function_calls:
            return {
                "reply": "\n".join(text_parts).strip() or "(empty reply)",
                "tool_calls": tool_calls_made,
            }

        # Execute every requested tool call and send results back.
        tool_response_parts = []
        for fc in function_calls:
            name = fc.name
            args = {}
            try:
                # fc.args is a Mapping-like proto; turn it into a plain dict
                args = dict(fc.args) if fc.args else {}
            except Exception:
                args = {}

            result = await run_tool(name, args, db)
            tool_calls_made.append({"tool": name, "input": args, "output": result})

            tool_response_parts.append({
                "function_response": {
                    "name": name,
                    "response": {"result": result},
                }
            })

        current_input = tool_response_parts  # feed back tool results next loop

    return {
        "reply": "Sorry, agent ne bohat zyada tool calls kar liye. Phir try karein.",
        "tool_calls": tool_calls_made,
    }
