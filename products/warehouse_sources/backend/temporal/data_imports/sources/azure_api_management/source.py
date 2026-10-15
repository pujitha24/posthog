from typing import Optional, cast

import structlog

from products.warehouse_sources.backend.facade.codebase_detection import NOT_DETECTABLE
from products.warehouse_sources.backend.facade.source_config import (
    DataWarehouseSourceCategory,
    ReleaseStatus,
    SourceConfig,
)
from products.warehouse_sources.backend.temporal.data_imports.sources.azure_log_analytics.azure_log_analytics import (
    AzureLogAnalyticsClient,
    AzureLogAnalyticsResumeConfig,
    azure_log_analytics_source,
    table_unreadable_reason,
    validate_credentials as validate_log_analytics_credentials,
)
from products.warehouse_sources.backend.temporal.data_imports.sources.azure_log_analytics.source import (
    NON_RETRYABLE_ERRORS,
    WORKSPACE_FIELDS,
    log_analytics_schemas,
)
from products.warehouse_sources.backend.temporal.data_imports.sources.common.base import FieldType, ResumableSource
from products.warehouse_sources.backend.temporal.data_imports.sources.common.canonical_descriptions import (
    CanonicalDescriptions,
)
from products.warehouse_sources.backend.temporal.data_imports.sources.common.registry import SourceRegistry
from products.warehouse_sources.backend.temporal.data_imports.sources.common.resumable import ResumableSourceManager
from products.warehouse_sources.backend.temporal.data_imports.sources.common.schema import SourceSchema
from products.warehouse_sources.backend.temporal.data_imports.sources.common.typings import SourceInputs, SourceResponse
from products.warehouse_sources.backend.temporal.data_imports.sources.generated_configs.azureapimanagement import (
    AzureApiManagementSourceConfig,
)
from products.warehouse_sources.backend.types import ExternalDataSourceType

# The resource-specific tables API Management writes when its diagnostic setting sends logs to a
# Log Analytics workspace.
API_MANAGEMENT_TABLES = [
    "ApiManagementGatewayLogs",
    "ApiManagementGatewayLlmLog",
    "ApiManagementWebSocketConnectionLogs",
]


@SourceRegistry.register
class AzureApiManagementSource(ResumableSource[AzureApiManagementSourceConfig, AzureLogAnalyticsResumeConfig]):
    # Applications call an API Management gateway over plain HTTP, so no package or key names it.
    codebase_detection = NOT_DETECTABLE

    lists_tables_without_credentials = True
    api_docs_url = "https://learn.microsoft.com/en-us/azure/api-management/monitor-api-management"

    @property
    def source_type(self) -> ExternalDataSourceType:
        return ExternalDataSourceType.AZUREAPIMANAGEMENT

    @property
    def connection_host_fields(self) -> list[str]:
        # `tenant_id` picks the directory the stored secret is exchanged in, and `workspace_id` picks
        # the workspace the token is spent on. Retargeting either must re-require the secret.
        return ["tenant_id", "workspace_id"]

    @property
    def get_source_config(self) -> SourceConfig:
        return SourceConfig(
            name=ExternalDataSourceType.AZUREAPIMANAGEMENT,
            category=DataWarehouseSourceCategory.ENGINEERING___MONITORING,
            label="Microsoft Azure API Management",
            keywords=["apim", "api gateway", "ai gateway"],
            releaseStatus=ReleaseStatus.ALPHA,
            caption="""Sync Azure API Management gateway logs into the PostHog Data warehouse: requests, LLM token usage, and WebSocket connections.

API Management must send its logs to a Log Analytics workspace. In the API Management instance, add a diagnostic setting that sends the gateway log categories to the workspace, with **Resource specific** as the destination table.

Register an app in Microsoft Entra ID, create a client secret for it, and give its service principal the **Log Analytics Reader** role on the workspace. Then enter the directory (tenant) ID, the application (client) ID, the secret value, and the workspace ID from the workspace's **Overview** page.""",
            iconPath="/static/services/azure_api_management.png",
            docsUrl="https://posthog.com/docs/cdp/sources/azure-api-management",
            fields=cast(list[FieldType], WORKSPACE_FIELDS),
        )

    def get_canonical_descriptions(self) -> CanonicalDescriptions:
        from products.warehouse_sources.backend.temporal.data_imports.sources.azure_api_management.canonical_descriptions import (
            CANONICAL_DESCRIPTIONS,
        )

        return CANONICAL_DESCRIPTIONS

    def get_non_retryable_errors(self) -> dict[str, str | None]:
        return NON_RETRYABLE_ERRORS

    def get_retryable_errors(self) -> set[str]:
        return {"Azure Log Analytics error (retryable)"}

    def get_schemas(
        self,
        config: AzureApiManagementSourceConfig,
        team_id: int,
        with_counts: bool = False,
        names: list[str] | None = None,
        force_refresh: bool = False,
        api_version: str | None = None,
    ) -> list[SourceSchema]:
        schemas = log_analytics_schemas(API_MANAGEMENT_TABLES)
        if names is not None:
            names_set = set(names)
            schemas = [s for s in schemas if s.name in names_set]
        return schemas

    def get_endpoint_permissions(
        self,
        config: AzureApiManagementSourceConfig,
        team_id: int,
        endpoints: list[str],
        api_version: str | None = None,
    ) -> dict[str, str | None]:
        client = AzureLogAnalyticsClient(
            config.tenant_id,
            config.client_id,
            config.client_secret,
            config.workspace_id,
            structlog.get_logger(__name__),
        )
        return {endpoint: table_unreadable_reason(client, endpoint) for endpoint in endpoints}

    def validate_credentials(
        self,
        config: AzureApiManagementSourceConfig,
        team_id: int,
        schema_name: Optional[str] = None,
        api_version: str | None = None,
    ) -> tuple[bool, str | None]:
        return validate_log_analytics_credentials(
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            client_secret=config.client_secret,
            workspace_id=config.workspace_id,
            table=schema_name,
        )

    def get_resumable_source_manager(
        self, inputs: SourceInputs
    ) -> ResumableSourceManager[AzureLogAnalyticsResumeConfig]:
        return ResumableSourceManager[AzureLogAnalyticsResumeConfig](inputs, AzureLogAnalyticsResumeConfig)

    def source_for_pipeline(
        self,
        config: AzureApiManagementSourceConfig,
        resumable_source_manager: ResumableSourceManager[AzureLogAnalyticsResumeConfig],
        inputs: SourceInputs,
    ) -> SourceResponse:
        return azure_log_analytics_source(
            tenant_id=config.tenant_id,
            client_id=config.client_id,
            client_secret=config.client_secret,
            workspace_id=config.workspace_id,
            table=inputs.schema_name,
            logger=inputs.logger,
            resumable_source_manager=resumable_source_manager,
            db_incremental_field_last_value=inputs.db_incremental_field_last_value
            if inputs.should_use_incremental_field
            else None,
        )
