"""Typed pipeline errors. Each carries a stable code and a message written for the end user."""


class PipelineError(Exception):
    code = "INTERNAL_ERROR"
    retryable = True  # whether the job should be retried automatically

    def __init__(self, message: str, *, code: str | None = None, retryable: bool | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if retryable is not None:
            self.retryable = retryable


class InvalidAudioError(PipelineError):
    """The file is corrupt, not audio, empty, or outside the allowed duration."""

    code = "INVALID_AUDIO"
    retryable = False


class StorageError(PipelineError):
    code = "STORAGE_ERROR"
    retryable = True


class TranscriptionError(PipelineError):
    code = "TRANSCRIPTION_FAILED"
    retryable = True


class ProviderAuthError(PipelineError):
    """Bad API key or exhausted credits - retrying will not help."""

    code = "PROVIDER_AUTH"
    retryable = False


class SummaryError(PipelineError):
    code = "SUMMARY_FAILED"
    retryable = True


class RecordingGone(Exception):
    """The recording was deleted while being processed; stop quietly."""
