"""How a failed Lightsail call is told to the user: ``LightsailError`` and its fixed reasons."""

from __future__ import annotations

from botocore import exceptions as boto_exc

from cnsr.errors import CnsrError

NOT_FOUND = "NotFoundException"

_OPERATIONS = {
    "create_client": "创建 AWS 客户端",
    "get_key_pairs": "查询 key pair",
    "import_key_pair": "导入 key pair",
    "get_blueprints": "查询系统镜像",
    "get_regions": "查询可用区",
    "get_instance": "查询实例",
    "create_instances": "创建实例",
    "get_instance_state": "查询实例状态",
    "get_static_ip": "查询静态 IP",
    "get_static_ips": "列出静态 IP",
    "allocate_static_ip": "申请静态 IP",
    "attach_static_ip": "绑定静态 IP",
    "detach_static_ip": "解绑静态 IP",
    "release_static_ip": "释放静态 IP",
    "put_instance_public_ports": "设置防火墙",
    "delete_instance": "删除实例",
}
# Errors whose AWS text may carry account details: only the fixed reason is shown.
_REASONS = {
    "AccessDeniedException": "没有权限执行这个操作，请检查 AWS 凭证对 Lightsail 的授权",
    "UnauthenticatedException": "AWS 凭证无效或已过期，请检查 access key",
    "UnrecognizedClientException": "AWS 凭证无效或已过期，请检查 access key",
    "InvalidClientTokenId": "AWS 凭证无效或已过期，请检查 access key",
    "InvalidSignatureException": "AWS 凭证无效，请检查 secret key",
    "SignatureDoesNotMatch": "AWS 凭证无效，请检查 secret key",
    "ExpiredToken": "AWS 凭证已过期",
    "ExpiredTokenException": "AWS 凭证已过期",
    "ThrottlingException": "请求太频繁，请稍后重试",
    "TooManyRequestsException": "请求太频繁，请稍后重试",
    "RequestLimitExceeded": "请求太频繁，请稍后重试",
    "AccountSetupInProgressException": "AWS 账号还在初始化，请稍后重试",
    "RegionSetupInProgressException": "这个区域还在初始化，请稍后重试",
    "ServiceException": "Lightsail 服务出错，请稍后重试",
    NOT_FOUND: "资源不存在",
}
# Missing or broken credentials, profile or config files (also raised while the client is made).
_CREDENTIAL_ERRORS = (
    boto_exc.NoCredentialsError,
    boto_exc.PartialCredentialsError,
    boto_exc.CredentialRetrievalError,
    boto_exc.UnknownCredentialError,
    boto_exc.ProfileNotFound,
    boto_exc.InvalidConfigError,
    boto_exc.ConfigNotFound,
    boto_exc.ConfigParseError,
    boto_exc.SSOError,
    boto_exc.TokenRetrievalError,
    boto_exc.NoAuthTokenError,
)
_NETWORK_ERRORS = (boto_exc.ConnectionError, boto_exc.HTTPClientError)


class LightsailError(CnsrError):
    """A Lightsail call that failed.

    ``operation`` is the boto3 method, ``code`` the AWS error code ('' when no response
    arrived) and ``aws_message`` the AWS text, which is kept out of the message when it may
    contain account details.
    """

    def __init__(self, operation: str, region: str, code: str, aws_message: str, reason: str):
        self.operation = operation
        self.code = code
        self.aws_message = aws_message
        label = _OPERATIONS.get(operation, operation)
        super().__init__(f"Lightsail 操作失败（{label}，{operation}，{region}）：{reason}")


def reason_for(code: str, message: str) -> str:
    """The reason shown for an AWS error code: a fixed text, or the AWS text when it is harmless."""
    return _REASONS.get(code) or f"AWS 返回 {code or '未知错误'}：{message or '没有详情'}"


def not_attached(error: LightsailError) -> bool:
    return error.code == "InvalidInputException" and "attach" in error.aws_message.lower()


def sdk_reason(exc: boto_exc.BotoCoreError) -> str:
    if isinstance(exc, _CREDENTIAL_ERRORS):
        return "没有找到可用的 AWS 凭证，或 AWS 配置有误。请检查 access key、环境变量 AWS_PROFILE，以及 ~/.aws 里的 config 和 credentials 文件"
    if isinstance(exc, _NETWORK_ERRORS):
        return "无法连接 AWS，请检查网络（若开着代理，先关闭）后重试"
    return f"AWS SDK 出错（{type(exc).__name__}）"
