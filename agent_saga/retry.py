from pydantic import BaseModel, Field
from typing import Literal, List, Type

class RetryPolicy(BaseModel):
    model_config = {
        "arbitrary_types_allowed": True
    }

    max_retries: int = Field(default=3, ge=0)
    backoff_type: Literal["linear", "exponential"] = Field(default="exponential")
    base_delay: float = Field(default=1.0, gt=0.0)
    max_delay: float = Field(default=60.0, gt=0.0)
    retry_on: List[Type[BaseException]] = Field(default_factory=lambda: [Exception])
    exclude_exceptions: List[Type[BaseException]] = Field(default_factory=list)

    def calculate_delay(self, attempt: int) -> float:
        """Calculate the backoff delay for a given attempt index (0-indexed)."""
        if self.backoff_type == "linear":
            delay = self.base_delay * (attempt + 1)
        else:
            delay = self.base_delay * (2 ** attempt)
        return min(delay, self.max_delay)
