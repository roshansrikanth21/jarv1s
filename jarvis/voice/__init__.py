"""jarvis.voice — the speech interaction stack, one engine per module.

    audio_in   microphone capture (RawInputStream → 16 kHz mono int16, 32 ms frames)
    vad        streaming Silero VAD (the single source of truth for "is someone speaking")
    wakeword   openWakeWord "hey jarvis" acoustic detector (no STT needed to wake)
    stt        faster-whisper transcription of already-endpointed speech
    tts        local Piper (default) / Edge neural TTS, sentence-streamed, played by the backend
    pipeline   the conversation state machine that wires them together

The design follows the architecture shared by mature open-source voice assistants
(Home Assistant Assist / Wyoming, RealtimeSTT/RealtimeTTS, GLaDOS, pipecat): an acoustic
wake word, a single streaming VAD for endpointing, a warm ASR model fed pre-segmented
speech, and local TTS streamed sentence-by-sentence with interruption.
"""
