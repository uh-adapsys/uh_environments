#!/usr/bin/env python

'''
Created on 12 Mar 2013

@author: nathan
'''
from collections import namedtuple

from threading import Thread, current_thread
import math
import time

from controller import Robot

try:
    import os
    path = os.path.dirname(os.path.realpath(__file__))
    if 'ROS_PACKAGE_PATH' not in os.environ:
        os.environ['ROS_PACKAGE_PATH'] = ''
    if os.environ['ROS_PACKAGE_PATH'].find(path) == -1:
        os.environ['ROS_PACKAGE_PATH'] = ':'.join((path, os.environ['ROS_PACKAGE_PATH']))
    roslib = __import__('roslib', globals(), locals())
    roslib.load_manifest('sunflowerController')
except:
    import logging
    logger = logging.getLogger()
    if logger.handlers:
        logging.getLogger().error(
            'Unable to load roslib, fatal error', exc_info=True)
    else:
        import sys
        import traceback
        print >> sys.stderr, 'Unable to load roslib, fatal error'
        print >> sys.stderr, traceback.format_exc()
    exit(1)
else:
    import rospy
    import sf_controller_msgs.msg
    import actionlib
    from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, Twist
    from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
    from nav_msgs.msg import Odometry
    # from p2os_msgs.msg import SonarArray
    from sensor_msgs.msg import LaserScan, JointState
    from dynamixel_msgs.msg import JointState as DynJointState
    from tf import TransformBroadcaster
    from tf.transformations import quaternion_from_euler


_states = {
    0: 'PENDING',
    'PENDING': 0,
    1: 'ACTIVE',
    'ACTIVE': 1,
    2: 'PREEMPTED',
    'PREEMPTED': 2,
    3: 'SUCCEEDED',
    'SUCCEEDED': 3,
    4: 'ABORTED',
    'ABORTED': 4,
    5: 'REJECTED',
    'REJECTED': 5,
    6: 'PREEMPTING',
    'PREEMPTING': 6,
    7: 'RECALLING',
    'RECALLING': 7,
    8: 'RECALLED',
    'RECALLED': 8,
    9: 'LOST',
    'LOST': 9,
}


class Sunflower(Robot):

    _actionHandles = {}
    Location = namedtuple('Location', ['x', 'y', 'theta', 'timestamp'])
    DistanceScan = namedtuple('DistanceScan', [
                              'min_angle',
                              'max_angle',
                              'min_range',
                              'max_range',
                              'scan_time',
                              'ranges'])

    # TODO: These should be in a config file
    # Speed limits from navigation files
    _translationSpeed = [-0.2, 0.4]
    _rotationSpeed = [-0.8, 0.8]

    def __init__(self, name, namespace='/'):
        super(Sunflower, self).__init__()
        self._namespace = namespace.rstrip('/') + '/'
        self._timeStep = int(self.getBasicTimeStep())
        self._actionName = name
        self._as = actionlib.SimpleActionServer(
            self._actionName,
            sf_controller_msgs.msg.SunflowerAction,
            execute_cb=self.executeCB,
            auto_start=False)
        self._as.start()
        try:
            # ROS Version >= Hydro
            self._cmdVel = rospy.Subscriber(
                self._namespace + 'cmd_vel',
                Twist,
                callback=self.cmdVelCB,
                queue_size=2)
        except:
            self._cmdVel = rospy.Subscriber(
                self._namespace + 'cmd_vel',
                Twist,
                callback=self.cmdVelCB)

        rospy.loginfo(
            'Started Sunflower Controller ActionServer on topic %s',
            self._actionName)
        self._location = None
        self._lastLocation = None
        self._rosTime = None
        self._servos = {}
        self._staticJoints = []
        self._sensors = {}
        self._sensorValues = {}
        self._leds = {}
        self.initialise()

    def _updateLocation(self):
        if self._sensors.get('gps', None) and self._sensors.get('compass', None):
            lX, lY, lZ = self._sensors['gps'].getValues()
            wX, _, wZ = self._sensors['compass'].getValues()

            # http://www.cyberbotics.com/reference/section3.13.php
            bearing = math.atan2(wX, wZ) - (math.pi / 2)
            x, y, _, rotation = self.webotsToRos(lX, lY, lZ, bearing)

            self._lastLocation = self._location
            self._location = Sunflower.Location(x, y, rotation, self.getTime())

    def _updateSonar(self):
        if self._sensors.get('sonar', None):
            self._sensorValues['sonar'] = map(
                lambda x: x.getValue(), self._sensors['sonar'])

    def _updateLaser(self):
        if self._sensors.get('frontLaser', None):
            fov = self._sensors['frontLaser'].getFov()
            ranges = self._sensors['frontLaser'].getRangeImage()
            ranges.reverse()
            maxRange = self._sensors['frontLaser'].getMaxRange()
            sampleRate = self._sensors['frontLaser'].getSamplingPeriod() / 1000

            self._sensorValues['frontLaser'] = Sunflower.DistanceScan(
                fov / -2,
                fov / 2,
                0,
                maxRange,
                sampleRate,
                ranges
            )

    def _publishOdomTransform(self, odomPublisher):
        if self._location:
            odomPublisher.sendTransform(
                (self._location.x, self._location.y, 0),
                quaternion_from_euler(0, 0, self._location.theta),
                self._rosTime,
                self._namespace + 'base_link',
                self._namespace + 'odom')

    def _publishLocationTransform(self, locationPublisher):
        if self._location:
            print self._location
            locationPublisher.sendTransform(
                (0, 0, 0),
                quaternion_from_euler(0, 0, 1.57079),
                self._rosTime,
                self._namespace + 'odom',
                'map',)

    def _publishLaserTransform(self, laserPublisher):
        if self._location:
            laserPublisher.sendTransform(
                (0.0, 0.0, 0.0),
                quaternion_from_euler(0, 0, 0),
                self._rosTime,
                self._namespace + 'scan_front',
                self._namespace + 'base_laser_front_link')

    def _publishOdom(self, odomPublisher):
        if self._location:
            msg = Odometry()
            msg.header.stamp = self._rosTime
            msg.header.frame_id = self._namespace + 'odom'
            msg.child_frame_id = self._namespace + 'base_link'

            msg.pose.pose.position.x = self._location.x
            msg.pose.pose.position.y = self._location.y
            msg.pose.pose.position.z = 0

            orientation = quaternion_from_euler(
                0, 0, self._location.theta)
            msg.pose.pose.orientation.x = orientation[0]
            msg.pose.pose.orientation.y = orientation[1]
            msg.pose.pose.orientation.z = orientation[2]
            msg.pose.pose.orientation.w = orientation[3]

            odomPublisher.publish(msg)
        else:
            rospy.logerr('Skipped updating odom! Last: %s, Cur: %s' %
                         (self._location, self._lastLocation))

    def _publishInitialPose(self, posePublisher):
        if self._location:
            msg = PoseWithCovarianceStamped()
            msg.header.stamp = self._rosTime
            msg.header.frame_id = 'map'
            msg.pose.pose.position.x = self._location.x
            msg.pose.pose.position.y = self._location.y
            msg.pose.pose.position.z = 0

            orientation = quaternion_from_euler(
                0, 0, self._location.theta + 1.57079)
            msg.pose.pose.orientation.x = orientation[0]
            msg.pose.pose.orientation.y = orientation[1]
            msg.pose.pose.orientation.z = orientation[2]
            msg.pose.pose.orientation.w = orientation[3]

            posePublisher.publish(msg)
            return True
        return False

    def _publishPose(self, posePublisher):
        if self._location:
            msg = Odometry()
            msg.header.stamp = self._rosTime
            msg.header.frame_id = self._namespace + 'odom'
            msg.child_frame_id = self._namespace + 'base_link'

            msg.pose.pose.position.x = self._location.x
            msg.pose.pose.position.y = self._location.y
            msg.pose.pose.position.z = 0

            orientation = quaternion_from_euler(
                0, 0, self._location.theta)
            msg.pose.pose.orientation.x = orientation[0]
            msg.pose.pose.orientation.y = orientation[1]
            msg.pose.pose.orientation.z = orientation[2]
            msg.pose.pose.orientation.w = orientation[3]

            if self._lastLocation:
                dt = (
                    self._location.timestamp - self._lastLocation.timestamp) / 1000
                msg.twist.twist.linear.x = (
                    self._location.x - self._lastLocation.x) / dt
                msg.twist.twist.linear.y = (
                    self._location.y - self._lastLocation.y) / dt
                msg.twist.twist.angular.x = (
                    self._location.theta - self._lastLocation.theta) / dt

            posePublisher.publish(msg)

    def _publishSonar(self, sonarPublisher):
        if self._sensorValues.get('sonar', None):
            # msg = SonarArray()
            # msg.header.stamp = self._rosTime

            # msg.ranges = self._sensorValues['sonar']
            # msg.ranges_count = len(self._sensorValues['sonar'])
            # sonarPublisher.publish(msg)
            pass

    def _publishLaser(self, laserPublisher):
        if self._sensorValues.get('frontLaser', None):
            laser_frequency = 40
            msg = LaserScan()
            msg.header.stamp = self._rosTime
            msg.header.frame_id = self._namespace + 'scan_front'

            msg.ranges = self._sensorValues['frontLaser'].ranges
            msg.angle_min = self._sensorValues['frontLaser'].min_angle
            msg.angle_max = self._sensorValues['frontLaser'].max_angle
            msg.angle_increment = abs(
                msg.angle_max - msg.angle_min) / len(msg.ranges)
            msg.range_min = self._sensorValues['frontLaser'].min_range
            msg.range_max = self._sensorValues['frontLaser'].max_range
            msg.scan_time = self._timeStep
            msg.time_increment = (msg.scan_time / laser_frequency /
                                  len(self._sensorValues['frontLaser'].ranges))
            laserPublisher.publish(msg)

    def _publishJoints(self, jointPublisher, dynamixelPublishers=None):
        # header:
        #   seq: 375
        #   stamp:
        #     secs: 1423791124
        #     nsecs: 372004985
        #   frame_id: ''
        # name: ['head_pan_joint', 'neck_lower_joint', 'swivel_hubcap_joint', 'base_swivel_joint', 'head_tilt_joint', 'neck_upper_joint']
        # position: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        # velocity: []
        # effort: []
        if (self._servos or self._staticJoints) and jointPublisher:
            msg = JointState()
            msg.header.stamp = self._rosTime
            names = []
            positions = []
            for (name, servo) in self._servos.iteritems():
                names.append('%s_joint' % name)
                # 'or 0.0' to prevent null from being published
                positions.append(servo.getPosition() or 0.0)
            for name in self._staticJoints:
                names.append('%s_joint' % name)
                # 'or 0.0' to prevent null from being published
                positions.append(0.0)

            msg.name = names
            msg.position = positions
            jointPublisher.publish(msg)
        if self._servos and dynamixelPublishers:
            # getComponentState of the sunflower class uses the dynamixel contoller status
            for (name, servo) in self._servos.iteritems():
                if name in dynamixelPublishers:
                    msg = DynJointState()
                    msg.header.stamp = self._rosTime
                    msg.name = name
                    msg.goal_pos = servo.getTargetposition()
                    msg.current_pos = servo.getPosition()
                    msg.load = servo.getTorqueFeedback()
                    dynamixelPublishers[name].publish(msg)

    def run(self):
        try:
            # ROS Version >= Hydro
            # sonarPublisher = rospy.Publisher(self._namespace + 'sonar', SonarArray, queue_size=2)
            odomPublisher = rospy.Publisher(self._namespace + 'odom', Odometry, queue_size=2)
            posePublisher = rospy.Publisher(self._namespace + 'pose', Odometry, queue_size=2)
            laserPublisher = rospy.Publisher(
                self._namespace + 'scan_front', LaserScan, queue_size=2)
            # clockPublisher = rospy.Publisher('/clock', Clock, queue_size=2)
            jointPublisher = rospy.Publisher(self._namespace + 'joint_states', JointState, queue_size=2)
            initialPosePublisher = rospy.Publisher(self._namespace + 'initialpose', PoseWithCovarianceStamped, queue_size=2)
            dynamixelPublishers = {name: rospy.Publisher(
                self._namespace + '%s_controller/state' % name, DynJointState, queue_size=2) for name in self._servos.iterkeys()}
        except:
            # sonarPublisher = rospy.Publisher(self._namespace + 'sonar', SonarArray)
            odomPublisher = rospy.Publisher(self._namespace + 'odom', Odometry)
            posePublisher = rospy.Publisher(self._namespace + 'pose', Odometry)
            laserPublisher = rospy.Publisher(self._namespace + 'scan_front', LaserScan)
            # clockPublisher = rospy.Publisher('/clock', Clock)
            jointPublisher = rospy.Publisher(self._namespace + 'joint_states', JointState)
            initialPosePublisher = rospy.Publisher(self._namespace + 'initialpose', PoseWithCovarianceStamped)
            dynamixelPublishers = {name: rospy.Publisher(self._namespace + '%s_controller/state' % name, DynJointState) for name in self._servos.iterkeys()}
        odomTransform = TransformBroadcaster()
#         locationTransform = TransformBroadcaster()
#         laserTransform = TransformBroadcaster()

        # Probably something wrong elsewhere, but we seem to need to publish
        # map->odom transform once to get sf_navigation to load
#         self._publishLocationTransform(locationTransform)
        # Start the ros clock
        initialposepublished = False

        rospy.loginfo("Main loop starting on thread: %s", current_thread().ident)

        while not rospy.is_shutdown() and self.step(self._timeStep) != -1:
            self._rosTime = rospy.Time.now()

            # Give time for ros to initialise
            if not initialposepublished and self._rosTime.secs >= 5:
                initialposepublished = self._publishInitialPose(initialPosePublisher)

            self._updateLocation()
            self._updateSonar()
            self._updateLaser()
            self._publishPose(posePublisher)
            self._publishOdom(odomPublisher)
            self._publishOdomTransform(odomTransform)
            # Published by robot_joint_publisher
            # self._publishLaserTransform(laserTransform)
            # Published by sf_navigation
            # self._publishLocationTransform(locationTransform)
            # self._publishSonar(sonarPublisher)
            self._publishLaser(laserPublisher)
            self._publishJoints(jointPublisher, dynamixelPublishers)

            # It appears that we have to call sleep for ROS to process messages
            time.sleep(0.0001)

    def webotsToRos(self, x, y, z, theta):
        rX = -z
        rY = -x
        rZ = y
        theta = -1 * theta
        return (rX, rY, rZ, theta)

    def initialise(self):
        numLeds = 3
        # numSonarSensors = 16

        self._leftWheel = self.getMotor('left_wheel')
        self._leftWheel.setPosition(float('+inf'))
        self._leftWheel.setVelocity(0)

        self._rightWheel = self.getMotor('right_wheel')
        self._rightWheel.setPosition(float('+inf'))
        self._rightWheel.setVelocity(0)

        self._staticJoints = [
            "base_swivel",
            "base_swivel_wheel",
            "base_left_wheel",
            "base_right_wheel"
        ]

        self._servos = {
            'tray': self.getMotor('tray'),
            'neck_lower': self.getMotor('neck_lower'),
            'neck_upper': self.getMotor('neck_upper'),
            'head_tilt': self.getMotor('head_tilt'),
            'head_pan': self.getMotor('head_pan'),
        }

        for servo in self._servos.values():
            servo.enablePosition(self._timeStep)

        self._sensors['frontLaser'] = self.getCamera('front_laser')
        self._sensors['frontLaser'].enable(self._timeStep)

        self._sensors['gps'] = self.getGPS('gps')
        self._sensors['gps'].enable(self._timeStep)

        self._sensors['compass'] = self.getCompass('compass')
        self._sensors['compass'].enable(self._timeStep)

        self._sensors['camera'] = self.getCamera('head_camera')
        if self._sensors['camera']:
            self._sensors['camera'].enable(self._timeStep)

        self._leds['body'] = self.getLED('light')

        self._leds['base'] = []
        for i in range(0, numLeds):
            led = self.getLED('red_led%s' % (i + 1))
            led.set(0)
            self._leds['base'].append(led)

        # self._sensors['sonar'] = []
        # for i in range(0, numSonarSensors):
        #    sensor = self.getDistanceSensor('so%s' % i)
        #    sensor.enable(self._timeStep)
        #    self._sensors['sonar'].append(sensor)

    def park(self):
        pass

    def executeCB(self, goal):
        # wrap 'done' in a dict for scoping reasons
        done = {'done': False}

        def doneCB(state=-1, result=None):
            print "Done: state:%s, result:%s" % (state, result)
            server_result = sf_controller_msgs.msg.SunflowerResult()
            server_result.result = state
            if result == 3:
                self._as.set_succeeded(server_result)
            elif result == 2:
                self._as.set_preempted(server_result)
            else:
                self._as.set_aborted(server_result)

            done['done'] = True

        if goal.component == 'light':
            self.setlight(goal.jointPositions, doneCB)
        elif goal.action == 'move':
            self.move(goal, doneCB)
        elif goal.action == 'init':
            self.init(goal.component, doneCB)
        elif goal.action == 'stop':
            self.stop(goal.component, doneCB)
        elif goal.action == 'park':
            self.park(doneCB)
        else:
            rospy.logwarn('Unknown action %s', goal.action)
            doneCB(4, None)

        while not done['done']:
            rospy.sleep(0.1)

    def init(self, name, doneCB):
        doneCB(3)

    def stop(self, name, doneCB):
        rospy.loginfo('%s: Stopping %s', self._actionName, name)
        if name == 'base':
            client = actionlib.SimpleActionClient(self._namespace + 'move_base', MoveBaseAction)
            client.wait_for_server()
            client.cancel_all_goals()

        doneCB(3)

    def setlight(self, color, doneCB):
        # Sunflower hardware only supports on/off states for RGB array
        # Webots selects the color as an array index of available colors
        # 3-bit color array is arranged in ascending binary order
        try:
            r = 0x4 if color[0] else 0
            g = 0x2 if color[1] else 0
            b = 0x1 if color[2] else 0
            # Webots color array is 1-indexed
            colorIndex = r + g + b + 1
            if self._leds['body']:
                self._leds['body'].set(colorIndex)
                doneCB(3)
            else:
                rospy.logerr('Unable to set color.  Body LED not found.')
                doneCB(-1)
        except Exception:
            rospy.logerr('Error setting color to: %s' % (color), exc_info=True)
            doneCB(-1)

    def move(self, goal, doneCB):
        joints = goal.jointPositions

        if(goal.namedPosition != '' and goal.namedPosition is not None):
            param = self._namespace + 'sf_controller/' + \
                goal.component + '/' + goal.namedPosition
            if(rospy.has_param(param)):
                joints = rospy.get_param(param)[0]

        rospy.loginfo('%s: Setting %s to %s',
                      self._actionName,
                      goal.component,
                      goal.namedPosition or joints)

        def onDone(state=-1, result=None):
            rospy.logdebug('%s: "%s to %s" Result:%s',
                           self._actionName,
                           goal.component,
                           goal.namedPosition or joints,
                           result)
            doneCB(state, result)

        try:
            if goal.component == 'base':
                self.navigate(goal, joints, onDone)
            elif goal.component == 'base_direct':
                self.moveBase(goal, joints, onDone)
            else:
                self.moveJoints(goal, joints, onDone)
        except Exception as e:
            rospy.logerr('Error occurred: %s' % e)
            onDone(-1)

    def moveBase(self, goal, positions, doneCB):
        LINEAR_RATE = math.pi / 2  # [rad/s]
        # WHEEL_DIAMETER = 0.195  # [m] From the manual
        WHEEL_RADIUS = 0.0975
        # BASE_SIZE = 0.3810  # [m] From the manual
        AXLE_LENGTH = 0.33  # [m] From WeBots Definition
        WHEEL_ROTATION = AXLE_LENGTH / (2 * WHEEL_RADIUS)

        rotation = round(positions[0], 4)
        linear = round(positions[1], 4)

        if not isinstance(rotation, (int, float)):
            rospy.logerr('Non-numeric rotation in list, aborting moveBase')
            return _states['ABORTED']

        rotRads = rotation * WHEEL_ROTATION
        linearRads = linear / WHEEL_RADIUS

        leftDuration = (rotRads + linearRads) / LINEAR_RATE
        rightDuration = ((-1 * rotRads) + linearRads) / LINEAR_RATE

        leftRate = LINEAR_RATE
        rightRate = LINEAR_RATE
        if leftDuration < 0:
            leftRate = -1 * leftRate
            leftDuration = abs(leftDuration)
        if rightDuration < 0:
            rightRate = -1 * rightRate
            rightDuration = abs(rightDuration)
        if leftDuration != rightDuration:
            if leftDuration < rightDuration:
                leftRate = leftRate * (leftDuration / rightDuration)
            else:
                rightRate = rightRate * (rightDuration / leftDuration)

        duration = max(leftDuration, rightDuration)
        rospy.loginfo('Setting rates: L=%s, R=%s for %ss' % (leftRate, rightRate, duration))
        start_time = self.getTime()
        end_time = start_time + duration
        while not rospy.is_shutdown():
            if self._as.is_preempt_requested():
                rospy.loginfo('%s: Preempted' % self._actionName)
                self._rightWheel.setVelocity(0)
                self._leftWheel.setVelocity(0)
                doneCB(2)
                return

            self._rightWheel.setVelocity(rightRate)
            self._leftWheel.setVelocity(leftRate)

            if self.getTime() >= end_time:
                break

        self._rightWheel.setVelocity(0)
        self._leftWheel.setVelocity(0)

        doneCB(3)

    def cmdVelCB(self, msg):
        # rospy.loginfo("cmdVelCB called on thread: %s", current_thread().ident)
        MAX_VEL = 5.24

        WHEEL_RADIUS = 0.0975
        AXLE_LENGTH = 0.33
        BASE_RADIUS = AXLE_LENGTH / 2

        vR = (msg.linear.x + (msg.angular.z * BASE_RADIUS * math.pi)) / WHEEL_RADIUS
        vL = (msg.linear.x - (msg.angular.z * BASE_RADIUS * math.pi)) / WHEEL_RADIUS

        if abs(vR) > MAX_VEL or abs(vL) > MAX_VEL:
            # scale vR/vL
            maxVel = max(abs(vR), abs(vL))
            scale = MAX_VEL / maxVel
            vR *= scale
            vL *= scale

        rospy.logdebug('Setting rates: L=%s, R=%s' % (vL, vR))
        self._rightWheel.setVelocity(vR)
        self._leftWheel.setVelocity(vL)

    def navigate(self, goal, positions, doneCB):
        rospy.loginfo("navigate called on thread: %s", current_thread().ident)
        pose = PoseStamped()
        pose.header.stamp = self._rosTime
        pose.header.frame_id = 'map'
        pose.pose.position.x = positions[0]
        pose.pose.position.y = positions[1]
        pose.pose.position.z = 0.0
        q = quaternion_from_euler(0, 0, positions[2])
        pose.pose.orientation.x = q[0]
        pose.pose.orientation.y = q[1]
        pose.pose.orientation.z = q[2]
        pose.pose.orientation.w = q[3]

        client = actionlib.SimpleActionClient(self._namespace + 'move_base', MoveBaseAction)
        client_goal = MoveBaseGoal()
        client_goal.target_pose = pose

        client.wait_for_server()
        rospy.loginfo('%s: Navigating to (%s, %s, %s)',
                      self._actionName,
                      positions[0],
                      positions[1],
                      positions[2])

        client.send_goal(client_goal, done_cb=doneCB)

    def moveJoints(self, goal, positions, doneCB):
        try:
            joint_names = rospy.get_param(
                self._namespace + 'sf_controller/%s/joint_names' %
                goal.component)
        except KeyError:
            # TODO: we're not publishing the dynamixel yaml configs for webots
            if goal.component == 'head':
                joint_names = ['head_pan', 'head_tilt', 'neck_upper', 'neck_lower']
            else:
                # assume component is a named joint
                joint_names = [goal.component, ]
        for i in range(0, len(joint_names)):
            servoName = joint_names[i]
            if servoName not in self._servos:
                rospy.logerr('Undefined joint %s', servoName)
                doneCB(4)
                return
            self._servos[servoName].setPosition(positions[i])

        inpos_threshold = 0.1
        waittime = 5
        waitstart = rospy.Time.now()
        while (rospy.Time.now() - waitstart).to_sec() <= waittime:
            done = True
            for i in range(0, len(joint_names)):
                current = self._servos[joint_names[i]].getPosition()
                target = positions[i]
                done = done and abs(current - target) <= inpos_threshold
            if done:
                doneCB(3)
                return

        doneCB(4)

if __name__ == '__main__':
    rospy.init_node('sf_controller')
    sf = Sunflower(rospy.get_name(), namespace="sunflower1_1")
    sf.run()
