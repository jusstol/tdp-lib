# Copyright 2022 TOSIT.IO
# SPDX-License-Identifier: Apache-2.0

import click

from tdp.cli.queries import get_latest_success_service_component_version_query
from tdp.cli.session import get_session_class
from tdp.cli.utils import (
    check_services_cleanliness,
    collections,
    database_dsn,
    dry,
    run_directory,
    validate,
    vars,
)
from tdp.core.dag import Dag
from tdp.core.deployment import (
    AnsibleExecutor,
    DeploymentPlan,
    DeploymentRunner,
    EmptyDeploymentPlanError,
    NothingToRestartError,
)
from tdp.core.models import StateEnum
from tdp.core.variables import ClusterVariables


@click.command(short_help="Restart required TDP services")
@dry
@collections
@database_dsn
@run_directory
@validate
@vars
def reconfigure(
    dry,
    collections,
    database_dsn,
    run_directory,
    validate,
    vars,
):
    if not vars.exists():
        raise click.BadParameter(f"{vars} does not exist")
    dag = Dag(collections)
    run_directory = run_directory.absolute() if run_directory else None

    ansible_executor = AnsibleExecutor(
        run_directory=run_directory,
        dry=dry,
    )

    session_class = get_session_class(database_dsn)
    with session_class() as session:
        latest_success_service_component_version = session.execute(
            get_latest_success_service_component_version_query()
        ).all()
        service_component_deployed_version = map(
            lambda result: result[1:], latest_success_service_component_version
        )
        cluster_variables = ClusterVariables.get_cluster_variables(
            collections, vars, validate=validate
        )
        check_services_cleanliness(cluster_variables)

        deployment_runner = DeploymentRunner(
            collections, ansible_executor, cluster_variables
        )
        try:
            deployment_plan = DeploymentPlan.from_reconfigure(
                dag, cluster_variables, service_component_deployed_version
            )
        except NothingToRestartError:
            click.echo("Nothing needs to be restarted")
            return
        except EmptyDeploymentPlanError:
            raise click.ClickException(
                f"Component(s) don't have any operation associated to restart (excluding noop). Nothing to restart."
            )

        deployment_iterator = deployment_runner.run(deployment_plan)
        if dry:
            for _ in deployment_iterator:
                pass
        else:
            session.add(deployment_iterator.log)
            # insert pending deployment log
            session.commit()
            for operation_log, service_component_log in deployment_iterator:
                if operation_log is not None:
                    session.add(operation_log)
                if service_component_log is not None:
                    session.add(service_component_log)
                session.commit()
            # notify sqlalchemy deployment log has been updated
            session.merge(deployment_iterator.log)
            session.commit()
        if deployment_iterator.log.state != StateEnum.SUCCESS:
            raise click.ClickException(
                (
                    "Deployment didn't finish with success: "
                    f"final state {deployment_iterator.log.state}"
                )
            )
