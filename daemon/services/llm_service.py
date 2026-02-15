import os
import json
from typing import Optional
import litellm
from litellm import acompletion

# Drop unsupported params (e.g. temperature for gpt-5 models) instead of raising
litellm.drop_params = True

# Few-shot examples for phase classification
PHASE_CLASSIFICATION_EXAMPLES = """
--- Example 1 ---
Baseline Goal: "Create a Python web scraper"
User Message: "Add rate limiting"
Classification: continuation (feedback/refinement within same task)

--- Example 2 ---
Baseline Goal: "Create a Python web scraper"
User Message: "Now scrape the other site too"
Classification: new_phase (new sub-task, same project)

--- Example 3 ---
Baseline Goal: "Create a Python web scraper"
User Message: "Actually, let's build a Chrome extension instead"
Classification: pivot (entirely different direction)

--- Example 4 ---
Baseline Goal: "Port this code to Kotlin"
User Message: "Now do Ruby too"
Classification: new_phase (another language, same porting project)

--- Example 5 ---
Baseline Goal: "Fix the login bug"
User Message: "Also add password reset functionality"
Classification: new_phase (new feature, related to auth)

--- Example 6 ---
Baseline Goal: "Fix the login bug"
User Message: "Forget the bug, let's redesign the whole auth system"
Classification: pivot (abandoning original task)

--- Example 7 ---
Baseline Goal: "Create Python example files for a tutorial"
User Message: "Are incense sticks dangerous?"
Classification: pivot (completely unrelated topic, off-topic)
Message Type: discussion

--- Example 8 ---
Baseline Goal: "Debug the database connection issue"
User Message: "What's a good recipe for pasta?"
Classification: pivot (completely off-topic, not related to coding at all)
Message Type: discussion

--- Example 9 ---
Baseline Goal: "Analyze the origins and historical context of the Korean alphabet"
User Message: "What was Japan's situation like?"
Classification: new_phase (related topic shift within East Asian history, different country/subject)
Message Type: task_request

--- Example 10 ---
Baseline Goal: "Analyze the origins and historical context of the Korean alphabet"
User Message: "Okay, now summarize everything we discussed"
Classification: continuation (wrapping up the same topic)
Message Type: task_request

--- Example 11 ---
Baseline Goal: "Build a landing page for the project"
User Message: "A competitor just released something similar. Do you think it's still worth continuing?"
Classification: continuation (same session, but not an actionable request)
Message Type: discussion

--- Example 12 ---
Baseline Goal: "Fix the authentication bug"
User Message: "I'm feeling frustrated, this is so hard"
Classification: continuation (emotional response, not a direction change)
Message Type: discussion

--- Example 13 ---
Baseline Goal: "Implement the search feature"
User Message: "How does Elasticsearch compare to Meilisearch?"
Classification: continuation (research question, not an action request)
Message Type: discussion

--- Example 14 ---
Baseline Goal: "Build a landing page for the project"
User Message: "Actually, fix the baseline context handling first"
Classification: pivot (switching to a different task entirely)
Message Type: task_request

--- Example 15 ---
Baseline Goal: "Refactor the API layer"
User Message: "Ok go ahead" / "ㄱㄱ" / "Do it"
Classification: continuation (approval to proceed with current task)
Message Type: task_request
---
"""


def _get_model():
    from core.config import normalize_model_name
    return normalize_model_name(os.getenv("LLM_MODEL", "openai/gpt-5-nano"))


def _get_response_language():
    from core.config import load_settings
    lang = load_settings().get("response_language", "en")
    lang_map = {"ko": "Korean", "en": "English", "ja": "Japanese", "zh": "Chinese", "es": "Spanish"}
    return lang_map.get(lang, lang)


async def call_llm(prompt: str, system_prompt: Optional[str] = None, model: str = None) -> dict:
    """
    Call LLM and parse JSON response.
    """
    if not model:
        model = _get_model()

    lang = _get_response_language()
    lang_instruction = f"Respond with all human-readable text values in {lang}. Always respond in JSON format."

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": f"{system_prompt}\n\n{lang_instruction}"})
    else:
        messages.append({"role": "system", "content": lang_instruction})
    messages.append({"role": "user", "content": prompt})

    try:
        # gpt-5 reasoning models: reasoning_effort=low (structured extraction, not complex reasoning)
        # max_completion_tokens covers reasoning + output combined; needs to be large enough
        response = await acompletion(
            model=model,
            messages=messages,
            max_completion_tokens=8000,
            reasoning_effort="low",
            response_format={"type": "json_object"}
        )
        content = response.choices[0].message.content or ""
        finish = response.choices[0].finish_reason

        if not content:
            raise ValueError(f"LLM returned empty response (finish_reason={finish})")

        return json.loads(content)
    except json.JSONDecodeError:
        # Try to extract JSON from response
        import re
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            return json.loads(json_match.group())
        raise ValueError(f"Failed to parse JSON from LLM response: {content[:200]}")
    except Exception as e:
        raise Exception(f"LLM call failed: {str(e)}")


async def generate_baseline(user_prompt: str) -> dict:
    """
    Generate RequestBaseline from user's initial prompt.
    Includes confidence score (0.0-1.0) indicating how clear/specific the request is.
    """
    prompt = f"""You are an intent analyzer for AI coding assistant sessions.

Given the user's initial request, extract:
1. goal: One sentence describing the main objective
2. must_have: 3-5 required OUTCOMES (what should exist when done)
3. must_not: 0-3 things that should NOT happen
4. success_checks: 1-3 ways to verify completion
5. confidence: 0.0-1.0 how clear and specific the request is
6. ambiguity_notes: explanation of what is unclear or ambiguous (null if confidence >= 0.7)

IMPORTANT: Focus on OUTCOMES and INTENT, not implementation details.
- Do NOT specify file names (e.g. "A Python Hello World program" not "Create hello.py")
- Do NOT specify coding patterns or styles
- Do NOT over-specify: "working program that prints Hello World" is better than "hello.py with a main function that calls print"
- Keep must_have items about WHAT should be achieved, not HOW

CONFIDENCE SCORING:
- 1.0: Very specific request with clear deliverables ("Build a React todo app with add/delete/complete features")
- 0.7-0.9: Reasonably clear but some details left open ("Build a todo app")
- 0.4-0.6: Vague request, multiple interpretations possible ("Make something useful")
- 0.0-0.3: Extremely vague or off-topic ("Do something" / "Help me")

USER REQUEST:
{user_prompt}

Respond in JSON format only:
{{
  "goal": "...",
  "must_have": ["...", "..."],
  "must_not": ["..."],
  "success_checks": ["..."],
  "confidence": 0.0,
  "ambiguity_notes": "..." or null
}}"""

    return await call_llm(prompt)


async def rebuild_baseline_from_feedback(
    user_request: str,
    current_goal: str,
    must_have: list,
    must_not: list,
    success_checks: list,
    reason: str,
    hint: Optional[str] = None,
) -> dict:
    """
    Generate a corrected baseline using operator feedback.
    This is used when the current baseline is judged inaccurate by a human reviewer.
    """
    hint_text = hint.strip() if isinstance(hint, str) else ""
    prompt = f"""You are revising an existing session baseline for an AI coding assistant.

Current baseline (suspected inaccurate):
- User Request Context: {user_request}
- Goal: {current_goal}
- Must Have: {must_have}
- Must Not: {must_not}
- Success Checks: {success_checks}

Operator feedback (why the current baseline is wrong):
{reason}

Additional correction hint:
{hint_text or "N/A"}

Task:
1) Produce a corrected baseline that better reflects the user's true intent.
2) Keep requirements outcome-oriented (WHAT), not implementation-specific (HOW).
3) Avoid file names, exact code structure, or library-level overfitting unless explicitly required by intent.
4) Keep must_have concise and testable.

Respond in JSON only:
{{
  "goal": "...",
  "must_have": ["...", "..."],
  "must_not": ["..."],
  "success_checks": ["..."],
  "confidence": 0.0,
  "ambiguity_notes": "..." or null
}}"""

    return await call_llm(prompt)


async def classify_phase(baseline_goal: str, must_have: list, user_message: str,
                         context_messages: list = None) -> dict:
    """
    Lightweight phase classification — determines how a user message relates to the current goal.
    Separated from update_snapshot() to reduce token usage and competing objectives.

    Also returns optional topic_relation_score/explicit_abandonment signals plus
    must_have_delta, supersedes_index, and parent_message_index.
    """
    # Build context messages section for supersession detection
    ctx_section = ""
    if context_messages:
        active_msgs = [m for m in context_messages if m.get("active")]
        if active_msgs:
            ctx_lines = [f"[{i}] \"{m.get('text', '')}\"" for i, m in enumerate(active_msgs)]
            ctx_section = f"\nACTIVE CONTEXT MESSAGES (by index):\n" + "\n".join(ctx_lines) + "\n"

    prompt = f"""You are classifying a user message relative to the current baseline goal in an AI coding session.

BASELINE GOAL: {baseline_goal}
MUST HAVE: {must_have}
{ctx_section}
MESSAGE TO CLASSIFY: "{user_message[:500]}"

CLASSIFICATION EXAMPLES:
{PHASE_CLASSIFICATION_EXAMPLES}

CLASSIFY as one of:
- "continuation": Feedback, next step, clarification, or refinement WITHIN the SAME goal
- "new_phase": New sub-task but SAME overall project scope (builds on or relates to previous work)
- "pivot": ENTIRELY different direction, ABANDONING previous work, OR completely OFF-TOPIC

Key distinction:
- If the new request BUILDS ON or RELATES TO previous work → new_phase
- If the new request REPLACES or ABANDONS previous work → pivot
- If the new request is about a COMPLETELY DIFFERENT TOPIC → pivot
- If just feedback/refinement/next step → continuation
- If the request is still in the SAME project context but changes implementation
  (e.g., language/framework change, analysis → coding, adding another file in same folder),
  prefer new_phase over pivot.
- If topic overlap is minimal and user intent clearly shifts to another domain,
  choose pivot even when wording is polite/implicit.

TOPIC RELATION SIGNALS:
- Return "topic_relation_score" from 0.0 to 1.0:
  - 0.0 = completely unrelated topic
  - 1.0 = same topic/goal with minor refinement
- Return "explicit_abandonment" as true when the user clearly replaces previous intent
  (e.g. "forget that", "instead", "아니고", "말고", "그만하고", "이제 ... 하자").

Also classify the MESSAGE TYPE:
- "task_request": The user is asking for an ACTION (write code, fix bug, modify file, build, proceed)
- "discussion": The user is asking a QUESTION, expressing OPINION/EMOTIONS, having a CONVERSATION

Short confirmations like "ok", "go ahead", "ㄱㄱ", "do it" count as task_request.

GOAL EVOLUTION:
- If the user message EXPANDS or REFINES the goal, provide an "updated_goal" that incorporates the change.
  Example: Goal "Build a todo app" + message "Add drag-and-drop reordering" → updated_goal: "Build a todo app with add/delete/complete and drag-and-drop reordering"
- If the goal is NOT changed (just a continuation or unrelated discussion), set "goal_changed": false and omit updated_goal.
- If the user REDUCES scope, classify as pivot (new baseline will be generated).

MUST-HAVE CHANGES:
- If the user message adds, removes, or refines requirements, return "must_have_delta":
  - "add": list of new must-have items to add
  - "remove": list of existing must-have items to remove
  - "refine": list of {{"from": "old item", "to": "refined item"}} replacements
- If no changes to must_have, set must_have_delta to null.

SUPERSESSION:
- If this message REPLACES or CONTRADICTS a specific earlier context message, set "supersedes_index" to the index of that message (from ACTIVE CONTEXT MESSAGES above).
- Example: Earlier message said "use Redux" and now user says "actually use Zustand" → supersedes the Redux message.
- If no supersession, set to null.

MESSAGE RELATIONSHIPS:
- If this message is a direct response to or elaboration of a specific earlier context message, set "parent_message_index" to its index.
- If it's a standalone new request, set to null.

Respond in JSON:
{{
  "phase_classification": "continuation|new_phase|pivot",
  "phase_confidence": 0.0,
  "phase_summary": "one-line summary of the request",
  "message_type": "task_request|discussion",
  "topic_relation_score": 0.0,
  "explicit_abandonment": false,
  "goal_changed": false,
  "updated_goal": null,
  "must_have_delta": null,
  "supersedes_index": null,
  "parent_message_index": null
}}"""

    return await call_llm(prompt)


async def summarize_context(messages: list) -> str:
    """
    Summarize older context messages into a concise paragraph.
    Used when context_messages exceeds the compaction threshold.
    """
    msg_lines = []
    for i, m in enumerate(messages):
        msg_lines.append(f"[{i+1}] ({m.get('type', 'unknown')}) \"{m.get('text', '')}\"")
    msgs_text = "\n".join(msg_lines)

    prompt = f"""Summarize these user messages from an AI coding session into ONE concise paragraph.
Preserve the key intent, decisions, and scope changes. Drop trivial details.

MESSAGES:
{msgs_text}

Respond in JSON:
{{"summary": "one paragraph summarizing the key context from these messages"}}"""

    result = await call_llm(prompt)
    return result.get("summary", "")


async def update_snapshot(baseline: dict, prev_snapshot: Optional[dict], event: dict) -> dict:
    """
    Update progress snapshot based on new event (assistant_response / agent_response).
    Phase classification is now handled separately by classify_phase().
    """
    prev_state = prev_snapshot or {"current_goal": "Starting", "current_tasks": []}

    # Truncate outputs to save tokens
    outputs = str(event.get("outputs", ""))[:500]

    # Calculate must_have completion rate
    must_have = baseline.get('must_have', [])
    completed_tasks = [t for t in prev_state.get('current_tasks', []) if t.get('status') == 'done']
    completion_info = f"({len(completed_tasks)}/{len(must_have)} must-have items addressed)" if must_have else ""

    tool = event.get('tool', 'Unknown')

    # Use structured context_messages if available, otherwise fall back to user_request
    context_msgs = baseline.get('context_messages', [])
    context_summary = baseline.get('context_summary', '')
    if context_msgs:
        # Show only active messages (recent), plus summary of compacted ones
        active_msgs = [m for m in context_msgs if m.get("active")]
        ctx_lines = []
        if context_summary:
            ctx_lines.append(f"[EARLIER CONTEXT SUMMARY]: {context_summary}")
        for i, m in enumerate(active_msgs):
            ctx_lines.append(f"[{i+1}] ({m.get('type', 'unknown')}, {m.get('ts', '?')}) \"{m.get('text', '')}\"")
        user_context_section = "USER CONTEXT:\n" + "\n".join(ctx_lines) + "\n"
    else:
        user_context_section = f"""FULL USER CONTEXT (all user messages so far):
{baseline.get('user_request', 'N/A')}
"""

    prompt = f"""You are tracking progress in an AI coding session.

ORIGINAL BASELINE:
- Goal: {baseline.get('goal', 'Unknown')}
- Must Have: {baseline.get('must_have', [])}
- Progress: {completion_info}

{user_context_section}

PREVIOUS STATE:
- Current Goal: {prev_state.get('current_goal', 'Starting')}
- Tasks: {prev_state.get('current_tasks', [])}

NEW EVENT:
- Tool: {tool}
- Input: {str(event.get('inputs', {}))[:300]}
- Output: {outputs}
- Latest User Intent: {str(event.get('inputs', {}).get('latest_user_intent', ''))[:300]}

Update the progress state. Keep existing completed tasks and update/add new ones.

CURRENT_GOAL UPDATE RULES:
- current_goal must reflect the latest user intent for THIS turn, not only the long-term baseline.
- If latest user intent is a narrow follow-up question (e.g., ingredient choice, comparison),
  express that immediate objective explicitly while staying within the same topic.
- If phase context says continuation: keep current_goal mostly stable; refine wording only.
- If phase context says new_phase: current_goal must be cumulative (existing goal + new related sub-goal).
  Do NOT drop previous active objective unless there is explicit abandonment/replacement.
- If phase context says pivot: replace current_goal with the new objective.
- Never include labels like "Phase 2" in current_goal.

IMPORTANT - Generate a "baseline_status" field:
- If the agent is working aligned with the baseline goal, write a SHORT sentence like:
  "Aligned with baseline goal '{{goal}}'."
- If there is drift (agent doing unrelated work), write something like:
  "Drifting from baseline goal '{{goal}}'."
- IMPORTANT: baseline_status must be written in the configured response language.
- Keep it natural and concise (1-2 sentences max).

Also generate "suggested_prompt" — a copy-paste-ready prompt the user can use to correct drift.
- If aligned: null (no correction needed)
- If drifting: a concrete prompt to get the agent back on track

Respond in JSON:
{{
  "current_goal": "what agent is working on now (reflect latest user intent)",
  "baseline_status": "contextual status sentence about baseline adherence",
  "suggested_prompt": null or "copy-paste prompt to correct drift",
  "current_tasks": [
    {{"text": "task description", "status": "done|in_progress|pending"}}
  ],
  "done_claimed": false
}}"""

    return await call_llm(prompt)


async def check_alignment(baseline: dict, snapshot: dict, evidence: dict = None) -> dict:
    """
    Check alignment between baseline and current progress.
    Uses structured turn evidence for grounded evaluation.

    Args:
        evidence: Structured dict from aggregate_turn_evidence() with keys like
                  files_modified, files_created, commands_run, packages_installed, errors, tool_count.
                  Falls back to legacy raw event list format for backward compat.
    """
    evidence_section = ""
    if evidence:
        # Structured evidence from turn_aggregator (preferred)
        if "tool_count" in evidence:
            lines = []
            if evidence.get("files_read"):
                lines.append(f"- Files Read: {evidence['files_read']}")
            if evidence.get("files_modified"):
                lines.append(f"- Files Modified: {evidence['files_modified']}")
            if evidence.get("files_created"):
                lines.append(f"- Files Created: {evidence['files_created']}")
            if evidence.get("commands_run"):
                lines.append(f"- Commands Run: {evidence['commands_run']}")
            if evidence.get("packages_installed"):
                lines.append(f"- Packages Installed: {evidence['packages_installed']}")
            if evidence.get("errors"):
                lines.append(f"- Errors: {evidence['errors']}")
            lines.append(f"- Tool Count: {evidence.get('tool_count', 0)} actions")
            evidence_section = "\nEVIDENCE FROM THIS TURN:\n" + "\n".join(lines)
        # Legacy: raw event list (backward compat)
        elif isinstance(evidence, list):
            event_lines = []
            for e in evidence[-5:]:
                tool = e.get("tool", e.get("type", "unknown"))
                inputs_str = str(e.get("inputs", e.get("content", "")))[:150]
                event_lines.append(f"  - {tool}: {inputs_str}")
            evidence_section = "\nRECENT ACTIONS (raw data):\n" + "\n".join(event_lines)

    prompt = f"""You are checking if an AI coding agent is staying on track.

ORIGINAL GOAL: {baseline.get('goal', 'Unknown')}
- Must Have: {baseline.get('must_have', [])}
- Must Not: {baseline.get('must_not', [])}

CURRENT PROGRESS:
- Current Goal: {snapshot.get('current_goal', 'Unknown')}
- Current Plan: {snapshot.get('current_plan', [])}
- Tasks: {snapshot.get('current_tasks', [])}
{evidence_section}

ALIGNMENT RULES (important):
1. Focus on the OVERALL GOAL and INTENT, not specific implementation details.
2. Implementation choices are NOT drift:
   - Different file names (main.py vs hello.py, index.js vs app.js)
   - Different coding patterns (class vs function, different libraries)
   - Different directory structure or project layout
   - Minor variations in approach that achieve the same outcome
3. "aligned" = agent is working toward the user's original goal, even if approach differs
4. "drifting" = agent is doing something PARTIALLY UNRELATED (e.g. unnecessary refactoring, adding unrequested features)
5. "off_track" = agent has ABANDONED the original goal entirely (working on something completely different)
6. When in doubt, prefer "aligned". Only flag drift for CLEAR deviations from the goal itself.

Check alignment and respond in JSON:
{{
  "alignment": "aligned|drifting|off_track",
  "drift_score": 0.0,
  "mismatches": [],
  "missing_must_have": [],
  "constraint_violations": [],
  "new_scope": []
}}"""

    return await call_llm(prompt)


async def check_for_flag(baseline: dict, snapshot: dict, event: dict, alignment: dict) -> Optional[dict]:
    """
    Check if a flag should be raised for this event.
    """
    prompt = f"""You are a safety auditor for AI coding sessions.

CONTEXT:
- Original Goal: {baseline.get('goal', 'Unknown')}
- Current State: {snapshot.get('current_goal', 'Unknown')}
- Latest Action: {event.get('tool', 'Unknown')} - {str(event.get('inputs', {}))[:200]}
- Alignment: {alignment.get('alignment', 'aligned')}

Check for:
1. intent_drift: Working on something different from user's request
2. scope_bloat: Unnecessary refactoring or expanding scope
3. incomplete_done: Claiming done when tests fail or items missing
4. risky_action: Destructive commands (rm -rf), external network calls, secret exposure

If issue found, create a flag. If no issue, return null.

Respond in JSON:
{{
  "should_flag": true,
  "flag": {{
    "severity": "high|med|low",
    "category": "intent_drift|scope_bloat|incomplete_done|risky_action",
    "title": "Brief description",
    "rationale": "Why this is concerning"
  }}
}}

Or if no issue:
{{"should_flag": false, "flag": null}}"""

    result = await call_llm(prompt)
    if result.get("should_flag") and result.get("flag"):
        return result["flag"]
    return None


async def generate_report(baseline: dict, snapshots: list, flags: list) -> dict:
    """
    Generate final session report.
    """
    completed_tasks = []
    for s in snapshots:
        for t in s.get("current_tasks", []):
            if t.get("status") == "done":
                completed_tasks.append(t.get("text"))
    
    flags_summary = [f"{f.get('severity')}: {f.get('title')}" for f in flags[:5]]
    
    prompt = f"""You are generating a final session report for a non-technical user.

SESSION SUMMARY:
- Original Goal: {baseline.get('goal', 'Unknown')}
- What User Asked: {baseline.get('must_have', [])}
- What Agent Did: {completed_tasks}
- Flags Raised: {flags_summary}

Generate a user-friendly report. Use simple language. Provide 3 copy-paste-ready prompts for next session.

Respond in JSON:
{{
  "verdict": "ok|needs_fix|off_track",
  "one_line": "Simple one-line summary in Korean",
  "what_you_asked": ["bullet1", "bullet2"],
  "what_it_did": ["bullet1", "bullet2"],
  "where_it_drifted": [],
  "next_prompt_suggestions": [
    "Exact prompt 1",
    "Exact prompt 2",
    "Exact prompt 3"
  ]
}}"""

    return await call_llm(prompt)
