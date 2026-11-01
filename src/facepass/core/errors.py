from collections.abc import Mapping


class ProblemError(Exception):
    """An error returned to the client as RFC 9457 problem details.

    `code` is the stable, machine-readable value that integrators branch on.
    """

    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail or title}")
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.headers = dict(headers or {})


class FaceRejectedError(ProblemError):
    """The image was readable but unusable. The end user can retry with a better photo."""

    def __init__(self, code: str, title: str, detail: str) -> None:
        super().__init__(422, code, title, detail)


class LivenessFailedError(FaceRejectedError):
    def __init__(self) -> None:
        super().__init__(
            "liveness_failed",
            "Liveness check failed",
            "The image looks like a photo or screen rather than a live person.",
        )


def not_found(resource: str) -> ProblemError:
    return ProblemError(404, f"{resource}_not_found", f"{resource.capitalize()} not found")


def unauthorized(detail: str) -> ProblemError:
    return ProblemError(
        401, "unauthorized", "Unauthorized", detail, headers={"WWW-Authenticate": "Bearer"}
    )
