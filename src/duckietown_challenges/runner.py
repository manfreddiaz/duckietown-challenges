#!/usr/bin/env python
import argparse
import getpass
import json
import logging
import os
import socket
import sys
import tempfile
import time
import traceback

from dt_shell.constants import DTShellConstants
from dt_shell.env_checks import check_executable_exists, InvalidEnvironment, check_docker_environment, \
    get_dockerhub_username
from dt_shell.remote import dtserver_work_submission, dtserver_report_job, ConnectionError
from . import __version__
from .challenge_results import read_challenge_results, ChallengeResults, ChallengeResultsStatus
from .constants import CHALLENGE_SOLUTION_OUTPUT_DIR, CHALLENGE_RESULTS_DIR, CHALLENGE_DESCRIPTION_DIR, \
    CHALLENGE_EVALUATION_OUTPUT_DIR

logging.basicConfig()
elogger = logging.getLogger('evaluator')
elogger.setLevel(logging.DEBUG)


def get_token_from_shell_config():
    path = os.path.join(os.path.expanduser(DTShellConstants.ROOT), 'config')
    data = open(path).read()
    config = json.loads(data)
    k = DTShellConstants.DT1_TOKEN_CONFIG_KEY
    if k not in config:
        msg = 'Please set a Duckietown Token using the command `dts tok set`.'
        raise Exception(msg)
    else:
        return config[k]


def dt_challenges_evaluator():
    elogger.info("dt-challenges-evaluator %s" % __version__)

    check_docker_environment()
    try:
        check_executable_exists('docker-compose')
    except InvalidEnvironment:
        msg = 'Could not find docker-compose. Please install it.'
        msg += '\n\nSee: https://docs.docker.com/compose/install/#install-compose'
        raise InvalidEnvironment(msg)

    parser = argparse.ArgumentParser()
    parser.add_argument("--continuous", action="store_true", default=False)
    parser.add_argument("--no-pull", dest='no_pull', action="store_true", default=False)
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    parsed = parser.parse_args()

    do_pull = not parsed.no_pull

    try:
        docker_username = get_dockerhub_username()
    except Exception:
        msg = 'Skipping push because docker_username is not set.'
        elogger.debug(msg)
        docker_username = None

    if parsed.continuous:

        timeout = 5.0  # seconds
        multiplier = 1.0
        max_multiplier = 10
        while True:
            multiplier = min(multiplier, max_multiplier)
            try:
                go_(None, do_pull, docker_username)
                multiplier = 1.0
            except NothingLeft:
                sys.stderr.write('.')
                # time.sleep(timeout * multiplier)
                # elogger.info('No submissions available to evaluate.')
            except ConnectionError as e:
                elogger.error(e)
                multiplier *= 1.5
            except Exception as e:
                msg = 'Uncaught exception: %s' % e
                elogger.error(msg)
                multiplier *= 1.5

            time.sleep(timeout * multiplier)

    else:
        submissions = parsed.extra

        if not submissions:
            submissions = [None]

        for submission_id in submissions:
            try:
                go_(submission_id, do_pull, docker_username)
            except NothingLeft:
                elogger.info('No submissions available to evaluate.')


class NothingLeft(Exception):
    pass


def go_(submission_id, do_pull, docker_username):
    token = get_token_from_shell_config()
    machine_id = socket.gethostname()

    evaluator_version = __version__

    process_id = str(os.getpid())

    res = dtserver_work_submission(token, submission_id, machine_id, process_id, evaluator_version)

    if 'job_id' not in res:
        msg = 'Could not find jobs: %s' % res['msg']
        raise NothingLeft(msg)
    job_id = res['job_id']

    elogger.info('Evaluating job %s' % job_id)
    # submission_id = result['submission_id']
    # parameters = result['parameters']
    # job_id = result['job_id']
    evaluation_container = None

    artifacts_image = size = None
    try:
        wd = tempfile.mkdtemp(prefix='tmp-duckietown-challenge-evaluator-')

        LAST = 'last'
        if os.path.lexists(LAST):
            os.unlink(LAST)
        os.symlink(wd, LAST)

        challenge_name = res['challenge_name']
        solution_container = res['parameters']['hash']
        challenge_parameters = res['challenge_parameters']
        print(challenge_parameters)
        evaluation_protocol = challenge_parameters['protocol']
        assert evaluation_protocol == 'p1'

        evaluation_container = challenge_parameters['container']

        UID = os.getuid()
        USERNAME = getpass.getuser()

        challenge_solution_output_dir = os.path.join(wd, CHALLENGE_SOLUTION_OUTPUT_DIR)
        challenge_results_dir = os.path.join(wd, CHALLENGE_RESULTS_DIR)
        challenge_description_dir = os.path.join(wd, CHALLENGE_DESCRIPTION_DIR)
        challenge_evaluation_output_dir = os.path.join(wd, CHALLENGE_EVALUATION_OUTPUT_DIR)

        for d in [challenge_solution_output_dir, challenge_results_dir, challenge_description_dir,
                  challenge_evaluation_output_dir]:
            os.makedirs(d)

        compose = """
        
    version: '3'
    services:
      solution:
      
        image: {solution_container}
        environment:
            username: {USERNAME}
            uid: {UID}
        
        volumes:
        - {challenge_solution_output_dir}:/{CHALLENGE_SOLUTION_OUTPUT_DIR}
        - {challenge_results_dir}:/{CHALLENGE_RESULTS_DIR}
        - {challenge_description_dir}:/{CHALLENGE_DESCRIPTION_DIR}
        - {challenge_evaluation_output_dir}:/{CHALLENGE_EVALUATION_OUTPUT_DIR}
        
      evaluator:
        image: {evaluation_container} 
        environment:
            username: {USERNAME}
            uid: {UID}
        
        volumes:
        - {challenge_solution_output_dir}:/{CHALLENGE_SOLUTION_OUTPUT_DIR}
        - {challenge_results_dir}:/{CHALLENGE_RESULTS_DIR}
        - {challenge_description_dir}:/{CHALLENGE_DESCRIPTION_DIR}
        - {challenge_evaluation_output_dir}:/{CHALLENGE_EVALUATION_OUTPUT_DIR}
    # volumes:
    #   CHALLENGE_SOLUTION_OUTPUT_DIR:
    #   CHALLENGE_EVALUATION_OUTPUT_DIR:
    #   CHALLENGE_DESCRIPTION_DIR:
    #   CHALLENGE_RESULTS_DIR:
    #   
    #   
    """.format(challenge_name=challenge_name,
               evaluation_container=evaluation_container,
               solution_container=solution_container,
               USERNAME=USERNAME,
               UID=UID,
               challenge_solution_output_dir=challenge_solution_output_dir,
               CHALLENGE_SOLUTION_OUTPUT_DIR=CHALLENGE_SOLUTION_OUTPUT_DIR,
               challenge_results_dir=challenge_results_dir,
               CHALLENGE_RESULTS_DIR=CHALLENGE_RESULTS_DIR,
               challenge_description_dir=challenge_description_dir,
               CHALLENGE_DESCRIPTION_DIR=CHALLENGE_DESCRIPTION_DIR,
               challenge_evaluation_output_dir=challenge_evaluation_output_dir,
               CHALLENGE_EVALUATION_OUTPUT_DIR=CHALLENGE_EVALUATION_OUTPUT_DIR)

        df = os.path.join(wd, 'Dockerfile')
        with open(df, 'w') as f:
            f.write("""
FROM scratch
COPY . /jobs/%s
            
            """ % job_id)

        dcfn = os.path.join(wd, 'docker-compose.yaml')

        print(compose)
        with open(dcfn, 'w') as f:
            f.write(compose)

        if do_pull:
            cmd = ['docker-compose', '-f', dcfn, 'pull']
            ret = os.system(" ".join(cmd))
            if ret != 0:
                msg = 'Could not run docker-compose pull.'
                raise Exception(msg)

        cmd = ['docker-compose', '-f', dcfn, 'up']
        ret = os.system(" ".join(cmd))
        if ret != 0:
            msg = 'Could not run docker-compose.'
            raise Exception(msg)

        # os.system('find %s' % wd)
        try:
            cr = read_challenge_results(wd)
        except Exception as e:
            msg = 'Could not read the challenge results:\n%s' % traceback.format_exc(e)
            elogger.error(msg)
            status = ChallengeResultsStatus.ERROR
            cr = ChallengeResults(status, msg, scores={})

        if docker_username is not None:
            import docker
            client = docker.from_env()

            tag = '%s/jobs:%s' % (docker_username, job_id)
            out = client.images.build(path=wd, tag=tag)
            for line in out:
                print(line)

            image = client.images.get(tag)
            artifacts_image = '%s@%s' % (tag, image.id)

            size = image.attrs['Size']

            print(artifacts_image)

            elogger.info('Pushing image %s' % tag)
            client.images.push(tag)
        else:
            size = artifacts_image = None


    except NothingLeft:
        raise
    except Exception as e:  # XXX
        msg = 'Uncaught exception:\n%s' % traceback.format_exc()
        elogger.error(msg)
        status = ChallengeResultsStatus.ERROR
        cr = ChallengeResults(status, msg, scores={})

    stats = cr.get_stats()
    if artifacts_image:
        stats['artifacts'] = dict(size=size, image=artifacts_image)

    dtserver_report_job(token,
                        job_id=job_id,
                        stats=stats,
                        result=cr.get_status(),
                        machine_id=machine_id,
                        process_id=process_id,
                        evaluation_container=evaluation_container,
                        evaluator_version=evaluator_version)
    #
    # process_id = data['process_id']
    # evaluator_version = data['evaluator_version']
    # evaluation_container = data['evaluation_container']
