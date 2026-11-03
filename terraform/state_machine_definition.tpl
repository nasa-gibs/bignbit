{
  "Comment": "Run BIG & BIT",
  "StartAt":"Get Dataset Configuration",
  "States":{
    "Get Dataset Configuration":{
      "Type":"Task",
      "Resource":"${GetDatasetConfigurationLambda}",
      "Parameters":{
        "cma":{
          "event.$":"$",
          "task_config":{
            "config_bucket_name":"${ConfigBucket}",
            "config_key_name.$":"States.Format('${ConfigDir}/{}.cfg', $.meta.collection.name)",
            "cumulus_message":{
              "input":"{$.payload}"
            }
          }
        }
      },
      "Retry":[
        {
          "ErrorEquals":[
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException"
          ],
          "IntervalSeconds":2,
          "MaxAttempts":6,
          "BackoffRate":2
        },
        {
          "ErrorEquals": [
            "Lambda.CodeArtifactUserPendingException"
          ],
          "IntervalSeconds": 60,
          "MaxAttempts": 5,
          "BackoffRate": 1
        },
        {
          "ErrorEquals": [
            "Lambda.Unknown"
          ],
          "BackoffRate": 2,
          "IntervalSeconds": 2,
          "MaxAttempts": 2
        },
        {
          "ErrorEquals": [
            "Sandbox.Timedout"
          ],
          "IntervalSeconds": 10,
          "MaxAttempts": 3,
          "BackoffRate": 2,
          "JitterStrategy": "FULL"
        }
      ],
      "Next":"Get Granule umm_json"
    },
    "Get Granule umm_json":{
      "Type":"Task",
      "Resource":"${GetGranuleUmmJsonLambda}",
      "Parameters":{
        "cma":{
          "event.$":"$",
          "task_config":{
            "cmr_environment":"{$.meta.cmr.cmrEnvironment}",
            "cumulus_message":{
              "input":"{$.payload}"
            }
          }
        }
      },
      "Retry":[
        {
          "ErrorEquals":[
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException",
            "ReadTimeout",
            "HTTPError"
          ],
          "IntervalSeconds":2,
          "MaxAttempts":16,
          "BackoffRate":2
        },
        {
          "ErrorEquals": [
            "Lambda.CodeArtifactUserPendingException"
          ],
          "IntervalSeconds": 60,
          "MaxAttempts": 5,
          "BackoffRate": 1
        },
        {
          "ErrorEquals": [
             "Lambda.Unknown"
          ],
          "BackoffRate": 2,
          "IntervalSeconds": 2,
          "MaxAttempts": 2
        },
        {
          "ErrorEquals": [
            "Sandbox.Timedout"
          ],
          "IntervalSeconds": 10,
          "MaxAttempts": 3,
          "BackoffRate": 2,
          "JitterStrategy": "FULL"
        }
      ],
      "Next":"Send to Harmony?"
    },
    "Send to Harmony?":{
      "Type":"Choice",
      "Choices":[
        {
          "And":[
            {
              "Variable":"$.payload.datasetConfigurationForBIG.config.sendToHarmony",
              "IsPresent":true
            },
            {
              "Variable":"$.payload.datasetConfigurationForBIG.config.sendToHarmony",
              "BooleanEquals":true
            }
          ],
          "Comment":"If sendToHarmony is true",
          "Next":"Get Collection Concept Id"
        }
      ],
      "Default":"Identify Image File"
    },
    "Identify Image File":{
      "Type":"Task",
      "Resource":"${IdentifyImageFileLambda}",
      "Parameters":{
        "cma":{
          "event.$":"$",
          "task_config":{
            "cumulus_message":{
              "input":"{$.payload}"
            }
          }
        }
      },
      "Retry":[
        {
          "ErrorEquals":[
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException"
          ],
          "IntervalSeconds":2,
          "MaxAttempts":6,
          "BackoffRate":2
        },
        {
          "ErrorEquals": [
            "Lambda.CodeArtifactUserPendingException"
          ],
          "IntervalSeconds": 60,
          "MaxAttempts": 5,
          "BackoffRate": 1
        },
        {
          "ErrorEquals": [
            "Lambda.Unknown"
          ],
          "BackoffRate": 2,
          "IntervalSeconds": 2,
          "MaxAttempts": 2
        },
        {
          "ErrorEquals": [
            "Sandbox.Timedout"
          ],
          "IntervalSeconds": 10,
          "MaxAttempts": 3,
          "BackoffRate": 2,
          "JitterStrategy": "FULL"
        }
      ],
      "Next":"Apply OPERA HLS Treatment?"
    },
    "Apply OPERA HLS Treatment?":{
      "Type":"Choice",
      "Choices":[
        {
          "And":[
            {
              "Variable":"$.payload.datasetConfigurationForBIG.config.operaHLSTreatment",
              "IsPresent":true
            },
            {
              "Variable":"$.payload.datasetConfigurationForBIG.config.operaHLSTreatment",
              "BooleanEquals":true
            }
          ],
          "Comment":"If operaHLSTreatment is true",
          "Next":"Apply OPERA HLS Treatment"
        }
      ],
      "Default":"Handle BIG Result"
    },
    "Apply OPERA HLS Treatment":{
      "Type":"Task",
      "Resource":"${ApplyOperaHLSTreatmentLambda}",
      "Parameters":{
        "cma":{
          "event.$":"$",
          "task_config":{
            "bignbit_staging_bucket": "${StagingBucket}",
            "cumulus_message":{
              "input":"{$.payload}"
            }
          }
        }
      },
      "Retry":[
        {
          "ErrorEquals":[
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException"
          ],
          "IntervalSeconds":2,
          "MaxAttempts":6,
          "BackoffRate":2
        },
        {
          "ErrorEquals": [
            "Lambda.CodeArtifactUserPendingException"
          ],
          "IntervalSeconds": 60,
          "MaxAttempts": 5,
          "BackoffRate": 1
        },
        {
          "ErrorEquals": [
            "Lambda.Unknown"
          ],
          "BackoffRate": 2,
          "IntervalSeconds": 2,
          "MaxAttempts": 2
        },
        {
          "ErrorEquals": [
            "Sandbox.Timedout"
          ],
          "IntervalSeconds": 10,
          "MaxAttempts": 3,
          "BackoffRate": 2,
          "JitterStrategy": "FULL"
        }
      ],
      "Next":"Handle BIG Result"
    },
    "Get Collection Concept Id":{
      "Type":"Task",
      "Resource":"${GetCollectionConceptIdLambda}",
      "Parameters":{
        "cma":{
          "event.$":"$",
          "task_config":{
            "collection_shortname":"{$.meta.collection.name}",
            "collection_version":"{$.meta.collection.version}",
            "cmr_provider":"{$.meta.cmr.provider}",
            "cmr_environment":"{$.meta.cmr.cmrEnvironment}",
            "cumulus_message":{
              "input":"{$.payload}"
            }
          }
        }
      },
      "Retry":[
        {
          "ErrorEquals":[
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException",
            "ReadTimeout",
            "HTTPError"
          ],
          "IntervalSeconds":2,
          "MaxAttempts":6,
          "BackoffRate":2
        },
        {
          "ErrorEquals": [
            "Lambda.CodeArtifactUserPendingException"
          ],
          "IntervalSeconds": 60,
          "MaxAttempts": 5,
          "BackoffRate": 1
        },
        {
          "ErrorEquals": [
            "Lambda.Unknown"
          ],
          "BackoffRate": 2,
          "IntervalSeconds": 2,
          "MaxAttempts": 2
        },
        {
          "ErrorEquals": [
            "Sandbox.Timedout"
          ],
          "IntervalSeconds": 10,
          "MaxAttempts": 3,
          "BackoffRate": 2,
          "JitterStrategy": "FULL"
        }
      ],
      "Next":"Convert Variables To PNG"
    },
    "Convert Variables To PNG":{
      "Type":"Map",
      "ItemsPath":"$.payload.datasetConfigurationForBIG.config.imgVariables",
      "ItemSelector":{
        "meta.$":"$.meta",
        "payload.$":"$.payload",
        "task_config.$":"$.task_config",
        "current_variable.$":"$$.Map.Item.Value"
      },
      "ItemProcessor":{
        "ProcessorConfig":{
          "Mode":"INLINE"
        },
        "StartAt":"Convert Variable For Each Projection",
        "States":{
          "Convert Variable For Each Projection":{
            "Type":"Map",
            "ItemsPath":"$.payload.datasetConfigurationForBIG.config.outputCrs",
            "ItemSelector":{
              "meta.$":"$.meta",
              "payload.$":"$.payload",
              "task_config.$":"$.task_config",
              "current_variable.$":"$.current_variable",
              "current_crs.$":"$$.Map.Item.Value"
            },
            "ItemProcessor":{
              "ProcessorConfig":{
                "Mode":"INLINE"
              },
              "StartAt":"Submit Harmony Job",
              "States":{
                  "Submit Harmony Job":{
                    "Type":"Task",
                    "Resource":"${SubmitHarmonyJobLambda}",
                    "Parameters":{
                      "cma":{
                        "event.$":"$",
                        "task_config":{
                          "granule":"{$.payload.granules[0]}",
                          "cmr_provider":"{$.meta.cmr.provider}",
                          "collection":"{$.meta.collection}",
                          "collection_concept_id":"{$.payload.collection_concept_id}",
                          "cmr_environment":"{$.meta.cmr.cmrEnvironment}",
                          "cmr_clientid":"{$.meta.cmr.clientId}",
                          "current_variable":"{$.current_variable}",
                          "current_crs":"{$.current_crs}",
                          "bignbit_staging_bucket": "${StagingBucket}",
                          "harmony_staging_path": "${HarmonyStagingPath}",
                          "big_config":"{$.payload.datasetConfigurationForBIG}",
                          "cumulus_message":{
                            "input":"{$.payload}"
                          }
                        }
                      }
                    },
                    "Retry": [
                      {
                        "ErrorEquals": [
                          "Lambda.ServiceException",
                          "Lambda.AWSLambdaException",
                          "Lambda.SdkClientException",
                          "Lambda.TooManyRequestsException"
                        ],
                        "IntervalSeconds": 2,
                        "MaxAttempts": 6,
                        "BackoffRate": 2
                      },
                      {
                        "ErrorEquals": [
                          "Lambda.CodeArtifactUserPendingException"
                        ],
                        "IntervalSeconds": 60,
                        "MaxAttempts": 5,
                        "BackoffRate": 1
                      },
                      {
                        "ErrorEquals": [
                          "Lambda.Unknown"
                        ],
                        "BackoffRate": 2,
                        "IntervalSeconds": 2,
                        "MaxAttempts": 2
                      }
                    ],
                    "Next":"Get Harmony Job Status"
                  },
                  "Get Harmony Job Status":{
                    "Type":"Task",
                    "Resource":"${GetHarmonyJobStatusLambda}",
                    "OutputPath":"$.payload",
                    "Parameters":{
                      "cma":{
                        "event.$":"$",
                        "task_config":{
                          "cmr_environment":"{$.meta.cmr.cmrEnvironment}",
                          "harmony_job":"{$.payload.harmony_job.job}",
                          "variable":"{$.current_variable.id}",
                          "current_crs":"{$.current_crs}",
                          "cumulus_message":{
                            "input":"{$.payload}"
                          }
                        }
                      }
                    },
                    "Retry":[
                      {
                        "ErrorEquals":[
                          "HarmonyJobIncompleteError",
                          "HarmonyTransientError"
                        ],
                        "IntervalSeconds":${HarmonyJobStatusIntervalSeconds},
                        "MaxAttempts":${HarmonyJobStatusMaxAttempts},
                        "BackoffRate":${HarmonyJobStatusBackoffRate},
                        "MaxDelaySeconds":${HarmonyJobStatusMaxDelaySeconds}
                      },
                      {
                        "ErrorEquals":[
                          "Lambda.ServiceException",
                          "Lambda.AWSLambdaException",
                          "Lambda.SdkClientException",
                          "Lambda.TooManyRequestsException"
                        ],
                        "IntervalSeconds":2,
                        "MaxAttempts":6,
                        "BackoffRate":2
                      },
                      {
                        "ErrorEquals": [
                          "Lambda.CodeArtifactUserPendingException"
                        ],
                        "IntervalSeconds": 60,
                        "MaxAttempts": 5,
                        "BackoffRate": 1
                      },
                      {
                        "ErrorEquals": [
                          "Lambda.Unknown"
                       ],
                        "BackoffRate": 2,
                        "IntervalSeconds": 2,
                        "MaxAttempts": 2
                      },
                      {
                        "ErrorEquals": [
                          "Sandbox.Timedout"
                        ],
                        "IntervalSeconds": 10,
                        "MaxAttempts": 3,
                        "BackoffRate": 2,
                        "JitterStrategy": "FULL"
                      }
                    ],
                    "Catch":[
                      {
                        "ErrorEquals":[
                          "HarmonyJobNoDataError"
                        ],
                        "ResultPath":"$.harmonyNoDataError",
                        "Next":"Handle No Data Result"
                      }
                    ],
                    "End":true
                  },
                  "Handle No Data Result":{
                    "Type":"Pass",
                    "Comment":"Handles cases where Harmony job succeeded but returned no data",
                    "Result":{},
                    "End":true
                  }
                }
            },
            "MaxConcurrency":5,
            "End":true
          }
        }
      },
      "MaxConcurrency":10,
      "ResultPath":"$.payload.big",
      "Next":"Handle BIG Result"
    },
    "Handle BIG Result":{
      "Type":"Task",
      "Resource":"${HandleBigResultLambda}",
      "Parameters":{
        "cma":{
          "event.$":"$",
          "task_config":{
            "bignbit_audit_bucket": "${BignbitAuditBucket}",
            "bignbit_audit_path": "${BignbitAuditPath}",
            "cmr_environment":"{$.meta.cmr.cmrEnvironment}",
            "cmr_provider": "{$.meta.cmr.provider}",
            "collection": "{$.meta.collection.name}",
            "cumulus_message":{
              "input":"{$.payload}"
            }
          }
        }
      },
      "Retry":[
        {
          "ErrorEquals":[
            "Lambda.ServiceException",
            "Lambda.AWSLambdaException",
            "Lambda.SdkClientException",
            "Lambda.TooManyRequestsException"
          ],
          "IntervalSeconds":2,
          "MaxAttempts":6,
          "BackoffRate":2
        },
        {
          "ErrorEquals": [
            "Lambda.CodeArtifactUserPendingException"
          ],
          "IntervalSeconds": 60,
          "MaxAttempts": 5,
          "BackoffRate": 1
        },
        {
          "ErrorEquals": [
            "Lambda.Unknown"
          ],
          "BackoffRate": 2,
          "IntervalSeconds": 2,
          "MaxAttempts": 2
        },
        {
          "ErrorEquals": [
            "Sandbox.Timedout"
          ],
          "IntervalSeconds": 10,
          "MaxAttempts": 3,
          "BackoffRate": 2,
          "JitterStrategy": "FULL"
        }
      ],
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.exception",
          "Next": "WorkflowFailed"
        }
      ],
      "Next":"Transfer Image Sets"
    },
    "Transfer Image Sets": {
      "Type": "Map",
      "InputPath": "$",
      "ItemsPath": "$.payload.pobit",
      "MaxConcurrency": 20,
      "ItemProcessor": {
        "ProcessorConfig": {
          "Mode": "INLINE"
        },
        "StartAt": "Send To GITC",
        "States": {
          "Send To GITC": {
            "Type": "Task",
            "Resource": "${SendToGITCLambda}",
            "Parameters": {
              "cma": {
                "event.$": "$",
                "task_config": {
                  "cumulus_message": {
                    "input": "{$}"
                  }
                }
              }
            },
            "TimeoutSeconds": 86400,
            "ResultPath": "$.gibs",
            "ResultSelector": {
              "cnmContent.$": "$.payload"
            },
            "Retry": [
              {
                "ErrorEquals": [
                  "Lambda.ServiceException",
                  "Lambda.AWSLambdaException",
                  "Lambda.SdkClientException",
                  "Lambda.TooManyRequestsException"
                ],
                "IntervalSeconds": 2,
                "MaxAttempts": 6,
                "BackoffRate": 2
              },
              {
                "ErrorEquals": [
                  "Lambda.CodeArtifactUserPendingException"
                ],
                "IntervalSeconds": 60,
                "MaxAttempts": 5,
                "BackoffRate": 1
              },
              {
                "ErrorEquals": [
                  "Lambda.Unknown"
                ],
                "BackoffRate": 2,
                "IntervalSeconds": 2,
                "MaxAttempts": 2
              },
              {
                "ErrorEquals": [
                  "Sandbox.Timedout"
                ],
                "IntervalSeconds": 10,
                "MaxAttempts": 3,
                "BackoffRate": 2,
                "JitterStrategy": "FULL"
              }
            ],
            "End": true
          }
        }
      },
      "ResultPath": "$.payload.pobit",
      "Catch": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "ResultPath": "$.exception",
          "Next": "WorkflowFailed"
        }
      ],
      "Retry": [
        {
          "ErrorEquals": [
            "States.ALL"
          ],
          "IntervalSeconds": 2,
          "MaxAttempts": 3
        }
      ],
      "Next": "WorkflowSucceeded"
    },
    "WorkflowSucceeded": {
      "Type": "Succeed"
    },
    "WorkflowFailed": {
      "Type": "Fail",
      "Cause": "Workflow failed"
    }
  }
}