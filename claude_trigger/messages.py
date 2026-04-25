import random

THINKING_MESSAGES = (
    "🤔 Claude is thinking...",
    "🧠 Claude is cooking something up...",
    "✨ Claude is vibing and computing...",
    "🛠️ Claude is on it...",
    "⚙️ Claude is processing...",
    "📡 Claude is consulting the cosmos...",
    "🔮 Claude is divining a response...",
    "🎯 Claude is taking aim...",
    "🪄 Claude is conjuring an answer...",
    "📚 Claude is flipping through the archives...",
    "🧩 Claude is fitting the pieces together...",
    "🌀 Claude is spinning up...",
    "☕ Claude is brewing a thought...",
    "🍳 Claude is cracking the problem...",
    "🐢 Claude is on its way (slow but steady)...",
    "🐇 Claude is hopping to it...",
    "🦉 Claude is pondering wisely...",
    "🦊 Claude is plotting cleverly...",
    "🚀 Claude is firing the boosters...",
    "🛰️ Claude is pinging the satellites...",
    "🎬 Claude is rolling tape...",
    "🎼 Claude is composing a reply...",
    "🧵 Claude is threading a response...",
    "🧶 Claude is unspooling thoughts...",
    "🪡 Claude is stitching together an answer...",
    "🧪 Claude is running the experiment...",
    "🧮 Claude is crunching numbers...",
    "📝 Claude is taking notes...",
    "🌱 Claude is growing a response...",
    "🪴 Claude is tending to the prompt...",
)

SIGNATURES = (
    "— _@claude_ 🤖",
    "— _sent by @claude_ 🤖",
    "— _@claude was here_ 🖊️",
    "— _written by @claude_ ✍️",
    "— _yours, @claude_ 💌",
    "— _@claude_ ✨",
    "— _@claude reporting in_ 📡",
    "— _@claude, signing off_ 🪪",
    "— _xoxo, @claude_ 💋",
    "— _@claude (the AI, not Steve)_ 🤖",
    "— _@claude on the case_ 🔍",
    "— _from the desk of @claude_ 📋",
    "— _@claude, at your service_ 🎩",
    "— _@claude says hi_ 👋",
    "— _piped through @claude_ 🪈",
    "— _@claude has spoken_ 🗣️",
    "— _delivered by @claude_ 📬",
    "— _@claude_ 🌟",
    "— _@claude, with love_ 💞",
    "— _@claude wrote this_ ✒️",
    "— _@claude's two cents_ 🪙",
    "— _@claude_ 🦾",
    "— _from @claude_ 🛠️",
    "— _@claude (silicon, not carbon)_ 🤖",
    "— _@claude logging off_ 👋",
)

THINKING_SET = frozenset(THINKING_MESSAGES)
SIGNATURE_MARKER = "— _@claude"  # every signature starts with this — quick filter


def random_thinking() -> str:
    return random.choice(THINKING_MESSAGES)


def random_signature() -> str:
    return random.choice(SIGNATURES)


def is_thinking_message(text: str) -> bool:
    return text in THINKING_SET


def is_claude_response(text: str) -> bool:
    """True if text looks like a past @claude response (has signature line)."""
    return f"\n\n{SIGNATURE_MARKER}" in text or text.startswith(SIGNATURE_MARKER)
