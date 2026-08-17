FROM mcr.microsoft.com/dotnet/aspnet:8.0 AS base
WORKDIR /app
EXPOSE 5001

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
ENV DOTNET_GCConserveMemory=9
COPY ["pubhealth-rfp-orchestrator.csproj", "."]
RUN dotnet restore "./pubhealth-rfp-orchestrator.csproj" --no-parallel
COPY . .
RUN dotnet publish "./pubhealth-rfp-orchestrator.csproj" -c Release -o /app/publish \
    /p:UseAppHost=false --no-parallel

FROM base AS final
WORKDIR /app
COPY --from=publish /app/publish .

# Use managed identity via DefaultAzureCredential — no secrets in image
ENV ASPNETCORE_URLS=http://+:5001
ENV ASPNETCORE_ENVIRONMENT=Production

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:5001/health || exit 1

ENTRYPOINT ["dotnet", "pubhealth-rfp-orchestrator.dll"]
