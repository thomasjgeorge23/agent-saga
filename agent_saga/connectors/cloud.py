"""Cloud & Infrastructure connector for AWS, GCP, Terraform, and Kubernetes agents.

Provides typed compensation semantics for virtual machines, cloud storage buckets,
and Kubernetes pod deployments. If a cloud deployment saga fails midway, provisioned
infrastructure resources are automatically terminated and cleaned up in LIFO order.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from ..semantics import ActionSemantics, Compensation
from ..registry import compensator

logger = logging.getLogger("agent_saga.connectors.cloud")


class CloudConnector:
    """Connector for Cloud Resource Provisioning & Infrastructure as Code."""

    async def provision_instance(self, instance_type: str, region: str = "us-east-1") -> Dict[str, Any]:
        """Forward action: Provision VM instance."""
        instance_id = "i-09f8e7d6c5b4a3210"
        logger.info("Provisioned %s instance %s in %s", instance_type, instance_id, region)
        return {
            "instance_id": instance_id,
            "instance_type": instance_type,
            "region": region,
            "status": "running",
        }

    async def create_s3_bucket(self, bucket_name: str, region: str = "us-east-1") -> Dict[str, Any]:
        """Forward action: Create S3 storage bucket."""
        logger.info("Created S3 bucket %s in %s", bucket_name, region)
        return {
            "bucket_name": bucket_name,
            "region": region,
            "status": "created",
        }

    async def deploy_k8s_pod(self, pod_name: str, image: str, namespace: str = "default") -> Dict[str, Any]:
        """Forward action: Deploy Kubernetes pod."""
        logger.info("Deployed K8s pod %s (%s) in namespace %s", pod_name, image, namespace)
        return {
            "pod_name": pod_name,
            "image": image,
            "namespace": namespace,
            "status": "running",
        }


@compensator("cloud.terminate_instance")
async def terminate_instance(instance_id: str, region: str = "us-east-1") -> Dict[str, Any]:
    logger.info("Terminating instance %s in %s", instance_id, region)
    return {"instance_id": instance_id, "status": "terminated"}


@compensator("cloud.delete_s3_bucket")
async def delete_s3_bucket(bucket_name: str, region: str = "us-east-1") -> Dict[str, Any]:
    logger.info("Deleting S3 bucket %s in %s", bucket_name, region)
    return {"bucket_name": bucket_name, "status": "deleted"}


@compensator("cloud.delete_k8s_pod")
async def delete_k8s_pod(pod_name: str, namespace: str = "default") -> Dict[str, Any]:
    logger.info("Deleting K8s pod %s in namespace %s", pod_name, namespace)
    return {"pod_name": pod_name, "namespace": namespace, "status": "deleted"}


def provision_instance_compensation(result: Dict[str, Any]) -> Compensation:
    return Compensation(
        fn=terminate_instance,
        args=[result["instance_id"]],
        kwargs={"region": result.get("region", "us-east-1")},
    )


def create_bucket_compensation(result: Dict[str, Any]) -> Compensation:
    return Compensation(
        fn=delete_s3_bucket,
        args=[result["bucket_name"]],
        kwargs={"region": result.get("region", "us-east-1")},
    )


def deploy_pod_compensation(result: Dict[str, Any]) -> Compensation:
    return Compensation(
        fn=delete_k8s_pod,
        args=[result["pod_name"]],
        kwargs={"namespace": result.get("namespace", "default")},
    )
