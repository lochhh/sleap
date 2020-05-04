import cattr
import os

from sleap import Labels, Video
from sleap.gui.dialogs.filedialog import FileDialog
from sleap.gui.dialogs.formbuilder import YamlFormWidget
from sleap.gui.learning import runners, utils, configs, datagen, receptivefield

from typing import Dict, List, Optional, Text

from PySide2 import QtWidgets, QtCore


# Debug option to skip the training run
SKIP_TRAINING = False

# List of fields which should show list of skeleton nodes
NODE_LIST_FIELDS = [
    "data.instance_cropping.center_on_part",
    "model.heads.centered_instance.anchor_part",
    "model.heads.centroid.anchor_part",
]


class LearningDialog(QtWidgets.QDialog):

    learningFinished = QtCore.Signal(int)

    def __init__(
        self,
        mode: Text,
        labels_filename: Text,
        labels: Optional[Labels] = None,
        skeleton: Optional["Skeleton"] = None,
        *args,
        **kwargs,
    ):
        super(LearningDialog, self).__init__()

        if labels is None:
            labels = Labels.load_file(labels_filename)

        if skeleton is None and labels.skeletons:
            skeleton = labels.skeletons[0]

        self.mode = mode
        self.labels_filename = labels_filename
        self.labels = labels
        self.skeleton = skeleton

        self._frame_selection = None

        self.current_pipeline = ""

        self.tabs = dict()
        self.shown_tab_names = []

        self._cfg_getter = configs.TrainingConfigsGetter.make_from_labels_filename(
            labels_filename=self.labels_filename
        )

        # Layout for buttons
        buttons = QtWidgets.QDialogButtonBox()
        self.cancel_button = buttons.addButton(QtWidgets.QDialogButtonBox.Cancel)
        self.save_button = buttons.addButton(
            "Save configuration files...", QtWidgets.QDialogButtonBox.ApplyRole
        )
        self.run_button = buttons.addButton(
            "Run", QtWidgets.QDialogButtonBox.AcceptRole
        )

        buttons_layout = QtWidgets.QHBoxLayout()
        buttons_layout.addWidget(buttons, alignment=QtCore.Qt.AlignTop)

        buttons_layout_widget = QtWidgets.QWidget()
        buttons_layout_widget.setLayout(buttons_layout)

        self.pipeline_form_widget = TrainingPipelineWidget(mode=mode, skeleton=skeleton)
        if mode == "training":
            tab_label = "Training Pipeline"
        elif mode == "inference":
            # self.pipeline_form_widget = InferencePipelineWidget()
            tab_label = "Inference Pipeline"
        else:
            raise ValueError(f"Invalid LearningDialog mode: {mode}")

        self.tab_widget = QtWidgets.QTabWidget()

        self.tab_widget.addTab(self.pipeline_form_widget, tab_label)
        self.make_tabs()

        # Layout for entire dialog
        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(self.tab_widget)
        layout.addWidget(buttons_layout_widget)

        self.setLayout(layout)

        # Default to most recently trained pipeline (if there is one)
        self.set_pipeline_from_most_recent()

        # Connect functions to update pipeline tabs when pipeline changes
        self.pipeline_form_widget.updatePipeline.connect(self.set_pipeline)
        self.pipeline_form_widget.emitPipeline()

        self.connect_signals()

        # Connect actions for buttons
        buttons.accepted.connect(self.run)
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(self.on_button_click)

        # Connect button for previewing the training data
        if "_view_datagen" in self.pipeline_form_widget.buttons:
            self.pipeline_form_widget.buttons["_view_datagen"].clicked.connect(
                self.view_datagen
            )

    def update_file_lists(self):
        self._cfg_getter.update()
        for tab in self.tabs.values():
            tab.update_file_list()

    @property
    def frame_selection(self) -> Dict[str, Dict[Video, List[int]]]:
        """
        Returns dictionary with frames that user has selected for learning.
        """
        return self._frame_selection

    @frame_selection.setter
    def frame_selection(self, frame_selection: Dict[str, Dict[Video, List[int]]]):
        """Sets options of frames on which to run learning."""
        self._frame_selection = frame_selection

        if "_predict_frames" in self.pipeline_form_widget.fields.keys():
            prediction_options = []

            def count_total_frames(videos_frames):
                if not videos_frames:
                    return 0
                count = 0
                for frame_list in videos_frames.values():
                    # Check for [x, Y] range given as X, -Y
                    # (we're not using typical [X, Y)-style range here)
                    if len(frame_list) == 2 and frame_list[1] < 0:
                        count += -frame_list[1] - frame_list[0]
                    elif frame_list != (0, 0):
                        count += len(frame_list)
                return count

            total_random = 0
            total_suggestions = 0
            total_user = 0
            random_video = 0
            clip_length = 0
            video_length = 0

            # Determine which options are available given _frame_selection
            if "random" in self._frame_selection:
                total_random = count_total_frames(self._frame_selection["random"])
            if "random_video" in self._frame_selection:
                random_video = count_total_frames(self._frame_selection["random_video"])
            if "suggestions" in self._frame_selection:
                total_suggestions = count_total_frames(
                    self._frame_selection["suggestions"]
                )
            if "user" in self._frame_selection:
                total_user = count_total_frames(self._frame_selection["user"])
            if "clip" in self._frame_selection:
                clip_length = count_total_frames(self._frame_selection["clip"])
            if "video" in self._frame_selection:
                video_length = count_total_frames(self._frame_selection["video"])

            # Build list of options

            if self.mode != "inference":
                prediction_options.append("nothing")
            prediction_options.append("current frame")

            option = f"random frames ({total_random} total frames)"
            prediction_options.append(option)
            default_option = option

            if random_video > 0:
                option = f"random frames in current video ({random_video} frames)"
                prediction_options.append(option)

            if total_suggestions > 0:
                option = f"suggested frames ({total_suggestions} total frames)"
                prediction_options.append(option)
                default_option = option

            if total_user > 0:
                option = f"user labeled frames ({total_user} total frames)"
                prediction_options.append(option)

            if clip_length > 0:
                option = f"selected clip ({clip_length} frames)"
                prediction_options.append(option)
                default_option = option

            prediction_options.append(f"entire video ({video_length} frames)")

            self.pipeline_form_widget.fields["_predict_frames"].set_options(
                prediction_options, default_option
            )

    def connect_signals(self):
        self.pipeline_form_widget.valueChanged.connect(self.on_tab_data_change)

        for head_name, tab in self.tabs.items():
            tab.valueChanged.connect(lambda n=head_name: self.on_tab_data_change(n))

    def disconnect_signals(self):
        self.pipeline_form_widget.valueChanged.disconnect()

        for head_name, tab in self.tabs.items():
            tab.valueChanged.disconnect()

    def make_tabs(self):
        heads = ("single_instance", "centroid", "centered_instance", "multi_instance")

        video = self.labels.videos[0] if self.labels else None

        for head_name in heads:
            self.tabs[head_name] = TrainingEditorWidget(
                video=video,
                skeleton=self.skeleton,
                head=head_name,
                cfg_getter=self._cfg_getter,
                require_trained=(self.mode == "inference"),
            )

    def adjust_data_to_update_other_tabs(self, source_data, updated_data=None):
        if updated_data is None:
            updated_data = source_data

        anchor_part = None
        set_anchor = False

        if "model.heads.centroid.anchor_part" in source_data:
            anchor_part = source_data["model.heads.centroid.anchor_part"]
            set_anchor = True
        elif "model.heads.centered_instance.anchor_part" in source_data:
            anchor_part = source_data["model.heads.centered_instance.anchor_part"]
            set_anchor = True

        if set_anchor:
            updated_data["model.heads.centroid.anchor_part"] = anchor_part
            updated_data["model.heads.centered_instance.anchor_part"] = anchor_part

    def update_tabs_from_pipeline(self, source_data):
        self.adjust_data_to_update_other_tabs(source_data)

        for tab in self.tabs.values():
            tab.set_fields_from_key_val_dict(source_data)

    def update_tabs_from_tab(self, source_data):
        data_to_transfer = dict()
        self.adjust_data_to_update_other_tabs(source_data, data_to_transfer)

        if data_to_transfer:
            for tab in self.tabs.values():
                tab.set_fields_from_key_val_dict(data_to_transfer)

    def on_tab_data_change(self, tab_name=None):
        self.disconnect_signals()

        if tab_name is None:
            # Move data from pipeline tab to other tabs
            source_data = self.pipeline_form_widget.get_form_data()
            self.update_tabs_from_pipeline(source_data)
        else:
            # Get data from head-specific tab
            source_data = self.tabs[tab_name].get_all_form_data()

            self.update_tabs_from_tab(source_data)

            # Update pipeline tab
            self.pipeline_form_widget.set_form_data(source_data)

        self._validate_pipeline()

        self.connect_signals()

    def get_most_recent_pipeline_trained(self) -> Text:
        recent_cfg_info = self._cfg_getter.get_first()
        if recent_cfg_info and recent_cfg_info.head_name:
            if recent_cfg_info.head_name in ("centroid", "centered_instance"):
                return "top-down"
            if recent_cfg_info.head_name in ("multi_instance"):
                return "bottom-up"
            if recent_cfg_info.head_name in ("single_instance"):
                return "single"
        return ""

    def set_pipeline_from_most_recent(self):
        recent_pipeline_name = self.get_most_recent_pipeline_trained()
        if recent_pipeline_name:
            self.pipeline_form_widget.current_pipeline = recent_pipeline_name

    def add_tab(self, tab_name):
        tab_labels = {
            "single_instance": "Single Instance Model Configuration",
            "centroid": "Centroid Model Configuration",
            "centered_instance": "Centered Instance Model Configuration",
            "multi_instance": "Bottom-Up Model Configuration",
        }
        self.tab_widget.addTab(self.tabs[tab_name], tab_labels[tab_name])
        self.shown_tab_names.append(tab_name)

    def remove_tabs(self):
        while self.tab_widget.count() > 1:
            self.tab_widget.removeTab(1)
        self.shown_tab_names = []

    def set_pipeline(self, pipeline: str):
        if pipeline != self.current_pipeline:
            self.remove_tabs()
            if pipeline == "top-down":
                self.add_tab("centroid")
                self.add_tab("centered_instance")
            elif pipeline == "bottom-up":
                self.add_tab("multi_instance")
            elif pipeline == "single":
                self.add_tab("single_instance")
        self.current_pipeline = pipeline

        self._validate_pipeline()

    def change_tab(self, tab_idx: int):
        print(tab_idx)

    def merge_pipeline_and_head_config_data(self, head_name, head_data, pipeline_data):
        for key, val in pipeline_data.items():
            # if key.starts_with("_"):
            #     continue
            if key.startswith("model.heads."):
                key_scope = key.split(".")
                if key_scope[2] != head_name:
                    continue
            head_data[key] = val

    def get_every_head_config_data(
        self, pipeline_form_data
    ) -> List[configs.ConfigFileInfo]:
        cfg_info_list = []

        for tab_name in self.shown_tab_names:
            trained_cfg_info = self.tabs[tab_name].trained_config_info_to_use
            if trained_cfg_info:
                trained_cfg_info.dont_retrain = trained_cfg_info
                cfg_info_list.append(trained_cfg_info)

            else:

                tab_cfg_key_val_dict = self.tabs[tab_name].get_all_form_data()

                self.merge_pipeline_and_head_config_data(
                    head_name=tab_name,
                    head_data=tab_cfg_key_val_dict,
                    pipeline_data=pipeline_form_data,
                )

                cfg = utils.make_training_config_from_key_val_dict(tab_cfg_key_val_dict)
                cfg_info = configs.ConfigFileInfo(config=cfg, head_name=tab_name)

                cfg_info_list.append(cfg_info)

        return cfg_info_list

    def get_selected_frames_to_predict(self, pipeline_form_data):
        frames_to_predict = dict()

        if self._frame_selection is not None:
            predict_frames_choice = pipeline_form_data.get("_predict_frames", "")
            if predict_frames_choice.startswith("current frame"):
                frames_to_predict = self._frame_selection["frame"]
            elif predict_frames_choice.startswith("random frames in current video"):
                frames_to_predict = self._frame_selection["random_video"]
            elif predict_frames_choice.startswith("random"):
                frames_to_predict = self._frame_selection["random"]
            elif predict_frames_choice.startswith("selected clip"):
                frames_to_predict = self._frame_selection["clip"]
            elif predict_frames_choice.startswith("suggested"):
                frames_to_predict = self._frame_selection["suggestions"]
            elif predict_frames_choice.startswith("entire video"):
                frames_to_predict = self._frame_selection["video"]
            elif predict_frames_choice.startswith("user"):
                frames_to_predict = self._frame_selection["user"]

            # Convert [X, Y+1) ranges to [X, Y] ranges for learning cli
            for video, frame_list in frames_to_predict.items():
                # Check for [A, -B] list representing [A, B) range
                if len(frame_list) == 2 and frame_list[1] < 0:
                    frame_list = (frame_list[0], frame_list[1] + 1)
                    frames_to_predict[video] = frame_list

        return frames_to_predict

    def get_items_for_inference(self, pipeline_form_data):
        predict_frames_choice = pipeline_form_data.get("_predict_frames", "")

        if predict_frames_choice.startswith("user"):
            # For inference on user labeled frames, we'll have a single
            # inference item.
            items_for_inference = runners.ItemsForInference(
                items=[
                    runners.DatasetItemForInference(
                        labels_path=self.labels_filename, frame_filter="user"
                    )
                ]
            )
        elif predict_frames_choice.startswith("suggested"):
            # For inference on all suggested frames, we'll have a single
            # inference item.
            items_for_inference = runners.ItemsForInference(
                items=[
                    runners.DatasetItemForInference(
                        labels_path=self.labels_filename, frame_filter="suggested"
                    )
                ]
            )
        else:
            # Otherwise, make an inference item for each video with list of frames.
            items_for_inference = runners.ItemsForInference.from_video_frames_dict(
                self.get_selected_frames_to_predict(pipeline_form_data)
            )
        return items_for_inference

    def _validate_pipeline(self):
        can_run = True

        if self.mode == "inference":
            # Make sure we have trained models for each required head
            for tab_name in self.shown_tab_names:
                tab = self.tabs[tab_name]
                if not tab.has_trained_config_selected:
                    can_run = False
                    break

        self.run_button.setEnabled(can_run)

    def view_datagen(self):
        pipeline_form_data = self.pipeline_form_widget.get_form_data()
        config_info_list = self.get_every_head_config_data(pipeline_form_data)
        datagen.show_datagen_preview(self.labels, config_info_list)
        self.hide()

    def on_button_click(self, button):
        if button == self.save_button:
            self.save()

    def run(self):
        """Run with current dialog settings."""

        pipeline_form_data = self.pipeline_form_widget.get_form_data()
        items_for_inference = self.get_items_for_inference(pipeline_form_data)

        config_info_list = self.get_every_head_config_data(pipeline_form_data)

        # Close the dialog now that we have the data from it
        self.accept()

        # Run training/learning pipeline using the TrainingJobs
        new_counts = runners.run_learning_pipeline(
            labels_filename=self.labels_filename,
            labels=self.labels,
            config_info_list=config_info_list,
            inference_params=pipeline_form_data,
            items_for_inference=items_for_inference,
        )

        self.learningFinished.emit(new_counts)

        if new_counts >= 0:
            QtWidgets.QMessageBox(
                text=f"Inference has finished. Instances were predicted on {new_counts} frames."
            ).exec_()

    def save(self):
        models_dir = os.path.join(os.path.dirname(self.labels_filename), "/models")
        output_dir = FileDialog.openDir(
            None, directory=models_dir, caption="Select directory to save scripts"
        )

        if not output_dir:
            return

        pipeline_form_data = self.pipeline_form_widget.get_form_data()
        items_for_inference = self.get_items_for_inference(pipeline_form_data)
        config_info_list = self.get_every_head_config_data(pipeline_form_data)

        runners.write_pipeline_files(
            output_dir=output_dir,
            labels_filename=self.labels_filename,
            config_info_list=config_info_list,
            inference_params=pipeline_form_data,
            items_for_inference=items_for_inference,
        )


class TrainingPipelineWidget(QtWidgets.QWidget):
    updatePipeline = QtCore.Signal(str)
    valueChanged = QtCore.Signal()

    def __init__(
        self, mode: Text, skeleton: Optional["Skeleton"] = None, *args, **kwargs
    ):
        super(TrainingPipelineWidget, self).__init__(*args, **kwargs)

        self.form_widget = YamlFormWidget.from_name(
            "pipeline_form", which_form=mode, title="Training Pipeline"
        )

        if hasattr(skeleton, "node_names"):
            for field_name in NODE_LIST_FIELDS:
                self.form_widget.set_field_options(
                    field_name, skeleton.node_names,
                )

        # Connect actions for change to pipeline
        self.pipeline_field = self.form_widget.form_layout.find_field("_pipeline")[0]
        self.pipeline_field.valueChanged.connect(self.emitPipeline)

        self.form_widget.form_layout.valueChanged.connect(self.valueChanged)

        self.setLayout(self.form_widget.form_layout)

    @property
    def fields(self):
        return self.form_widget.fields

    @property
    def buttons(self):
        return self.form_widget.buttons

    def get_form_data(self):
        return self.form_widget.get_form_data()

    def set_form_data(self, data):
        self.form_widget.set_form_data(data)

    def emitPipeline(self):
        val = self.current_pipeline
        self.updatePipeline.emit(val)

    @property
    def current_pipeline(self):
        pipeline_selected_label = self.pipeline_field.value()
        if "top-down" in pipeline_selected_label:
            return "top-down"
        if "bottom-up" in pipeline_selected_label:
            return "bottom-up"
        if "single" in pipeline_selected_label:
            return "single"
        return ""

    @current_pipeline.setter
    def current_pipeline(self, val):
        if val not in ("top-down", "bottom-up", "single"):
            raise ValueError(f"Cannot set pipeline to {val}")

        # Match short name to full pipeline name shown in menu
        for full_option_name in self.pipeline_field.option_list:
            if val in full_option_name:
                val = full_option_name
                break

        self.pipeline_field.setValue(val)
        self.emitPipeline()


class TrainingEditorWidget(QtWidgets.QWidget):
    """
    Dialog for viewing and modifying training profiles.

    Args:
        video: Video to use for receptive field preview
        skeleton: Skeleton to use for node option list
        head: If given, then only show configs with specified head name
        cfg_getter: Object to use for getting list of config files.
            If given, then menu of config files will be shown.
        require_trained: If True, then only show configs that are trained,
            and don't allow user to uncheck "use trained" setting.
    """

    valueChanged = QtCore.Signal()

    def __init__(
        self,
        video: Optional[Video] = None,
        skeleton: Optional["Skeleton"] = None,
        head: Optional[Text] = None,
        cfg_getter: Optional["TrainingConfigsGetter"] = None,
        require_trained: bool = False,
        *args,
        **kwargs,
    ):
        super(TrainingEditorWidget, self).__init__()

        self._video = video
        self._cfg_getter = cfg_getter
        self._cfg_list_widget = None
        self._receptive_field_widget = None
        self._use_trained_model = None
        self._require_trained = require_trained
        self.head = head

        yaml_name = "training_editor_form"

        self.form_widgets = dict()

        for key in ("model", "data", "augmentation", "optimization", "outputs"):
            self.form_widgets[key] = YamlFormWidget.from_name(
                yaml_name, which_form=key, title=key.title()
            )
            self.form_widgets[key].valueChanged.connect(self.emitValueChanged)

        self.form_widgets["model"].valueChanged.connect(self.update_receptive_field)
        self.form_widgets["data"].valueChanged.connect(self.update_receptive_field)

        if hasattr(skeleton, "node_names"):
            for field_name in NODE_LIST_FIELDS:
                form_name = field_name.split(".")[0]
                self.form_widgets[form_name].set_field_options(
                    field_name, skeleton.node_names,
                )

        if self._video:
            self._receptive_field_widget = receptivefield.ReceptiveFieldWidget(
                self.head
            )
            self._receptive_field_widget.setImage(self._video.test_frame)

        self._set_head()

        # Layout for header and columns
        layout = QtWidgets.QVBoxLayout()

        # Two column layout for config parameters
        col1_layout = QtWidgets.QVBoxLayout()
        col2_layout = QtWidgets.QVBoxLayout()
        col3_layout = QtWidgets.QVBoxLayout()

        col1_layout.addWidget(self.form_widgets["data"])
        col2_layout.addWidget(self.form_widgets["optimization"])
        col2_layout.addWidget(self.form_widgets["augmentation"])
        col3_layout.addWidget(self.form_widgets["model"])

        col_layout = QtWidgets.QHBoxLayout()
        col_layout.addWidget(self._layout_widget(col1_layout))
        col_layout.addWidget(self._layout_widget(col2_layout))
        col_layout.addWidget(self._layout_widget(col3_layout))

        if self._receptive_field_widget:
            col1_layout.addWidget(self._receptive_field_widget)

        # If we have an object which gets a list of config files,
        # then we'll show a menu to allow selection from the list.

        if self._cfg_getter:
            self._cfg_list_widget = configs.TrainingConfigFilesWidget(
                cfg_getter=self._cfg_getter,
                head_name=head,
                require_trained=require_trained,
            )
            self._cfg_list_widget.onConfigSelection.connect(
                self.acceptSelectedConfigInfo
            )
            # self._cfg_list_widget.setDataDict.connect(self.set_fields_from_key_val_dict)

            layout.addWidget(self._cfg_list_widget)

            # Add option for using trained model from selected config
            if self._require_trained:
                self._update_use_trained()
            else:
                self._use_trained_model = QtWidgets.QCheckBox("Use Trained Model")
                self._use_trained_model.setEnabled(False)
                self._use_trained_model.setVisible(False)

                self._use_trained_model.stateChanged.connect(self._update_use_trained)

                layout.addWidget(self._use_trained_model)

        layout.addWidget(self._layout_widget(col_layout))
        self.setLayout(layout)

    @staticmethod
    def _layout_widget(layout):
        widget = QtWidgets.QWidget()
        widget.setLayout(layout)
        return widget

    def emitValueChanged(self):
        self.valueChanged.emit()

        # When there's a config getter, we want to inform it that the data
        # has changed so that it can activate/update the "user" config
        # if self._cfg_list_widget:
        #     self._set_user_config()

    def acceptSelectedConfigInfo(self, cfg_info: configs.ConfigFileInfo):
        self._load_config(cfg_info)

        has_trained_model = cfg_info.has_trained_model
        if self._use_trained_model:
            self._use_trained_model.setVisible(has_trained_model)
            self._use_trained_model.setEnabled(has_trained_model)

        self.update_receptive_field()

    def update_receptive_field(self):
        data_form_data = self.form_widgets["data"].get_form_data()
        model_cfg = utils.make_model_config_from_key_val_dict(
            key_val_dict=self.form_widgets["model"].get_form_data()
        )

        rf_image_scale = data_form_data.get("data.preprocessing.input_scaling", 1.0)

        if self._receptive_field_widget:
            self._receptive_field_widget.setModelConfig(model_cfg, scale=rf_image_scale)
            self._receptive_field_widget.repaint()

    def update_file_list(self):
        self._cfg_list_widget.update()

    def _load_config_or_key_val_dict(self, cfg_data):
        if type(cfg_data) != dict:
            self._load_config(cfg_data)
        else:
            self.set_fields_from_key_val_dict(cfg_data)

    def _load_config(self, cfg_info: configs.ConfigFileInfo):
        if cfg_info is None:
            return

        cfg = cfg_info.config
        cfg_dict = cattr.unstructure(cfg)
        key_val_dict = utils.ScopedKeyDict.from_hierarchical_dict(cfg_dict).key_val_dict
        self.set_fields_from_key_val_dict(key_val_dict)

    # def _set_user_config(self):
    #     cfg_form_data_dict = self.get_all_form_data()
    #     self._cfg_list_widget.setUserConfigData(cfg_form_data_dict)

    def _update_use_trained(self, check_state=0):
        if self._require_trained:
            use_trained = True
        else:
            use_trained = check_state == QtCore.Qt.CheckState.Checked

        for form in self.form_widgets.values():
            form.set_enabled(not use_trained)

        # If user wants to use trained model, then reset form to match config
        if use_trained:
            cfg_info = self._cfg_list_widget.getSelectedConfigInfo()
            self._load_config(cfg_info)

        self._set_head()

    def _set_head(self):
        if self.head:
            self.set_fields_from_key_val_dict(
                {"_heads_name": self.head,}
            )

            self.form_widgets["model"].set_field_enabled("_heads_name", False)

    def set_fields_from_key_val_dict(self, cfg_key_val_dict):
        for form in self.form_widgets.values():
            form.set_form_data(cfg_key_val_dict)

        self._set_backbone_from_key_val_dict(cfg_key_val_dict)

    def _set_backbone_from_key_val_dict(self, cfg_key_val_dict):
        for key, val in cfg_key_val_dict.items():
            if key.startswith("model.backbone.") and val is not None:
                backbone_name = key.split(".")[2]
                self.set_fields_from_key_val_dict(dict(_backbone_name=backbone_name))
                break

    @property
    def trained_config_info_to_use(self) -> Optional[configs.ConfigFileInfo]:
        use_trained = False
        if self._require_trained:
            use_trained = True
        elif self._use_trained_model and self._use_trained_model.isChecked():
            use_trained = True

        if use_trained:
            return self._cfg_list_widget.getSelectedConfigInfo()
        return None

    @property
    def has_trained_config_selected(self) -> bool:
        cfg_info = self._cfg_list_widget.getSelectedConfigInfo()
        if cfg_info and cfg_info.has_trained_model:
            return True
        return False

    def get_all_form_data(self) -> dict:
        form_data = dict()
        for form in self.form_widgets.values():
            form_data.update(form.get_form_data())
        return form_data


def demo_training_dialog():
    app = QtWidgets.QApplication([])

    filename = "tests/data/json_format_v1/centered_pair.json"
    labels = Labels.load_file(filename)
    win = LearningDialog("training", labels_filename=filename, labels=labels)

    win.frame_selection = {"clip": {labels.videos[0]: (1, 2, 3, 4)}}
    # win.training_editor_widget.set_fields_from_key_val_dict({
    #     "_backbone_name": "unet",
    #     "_heads_name": "centered_instance",
    # })
    #
    # win.training_editor_widget.form_widgets["model"].set_field_enabled("_heads_name", False)

    win.show()
    app.exec_()


if __name__ == "__main__":
    demo_training_dialog()
