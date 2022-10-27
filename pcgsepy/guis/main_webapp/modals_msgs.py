privacy_policy_body = """If you agree, we will share your usage statistics for scientific purposes with [Araya Inc.](https://www.araya.org/en/), who developed the *AI Spaceship Generator* with the help of GoodAI.
We would like to understand your level of engagement with the generator and ask you for feedback in a Google form questionnaire in order to further improve the application.
You can use the application without agreeing to the [privacy policy](./assets/privacy_policy.pdf); in such case, we will not be collecting your usage statistics and you will not be prompted for feedback."""

privacy_policy_question = "Do you agree with the privacy policy?"

no_selection_error = "You must choose a spaceship from the grid on the left!"

end_of_experiment = "End of generations with the current configuration! Select the spaceship you like the most and download it, then fill out the next section of the questionnaire before proceeding."

end_of_userstudy = "The application will now switch to the user mode. You will be able to generate from a selected spaceship or automate generations by selecting the **Randomly generate spaceships**, as well as resetting the collection of spaceships."

spaceship_population_help = """
The population is the collection of all spaceships generated by the program. The spaceships are subdivided according to the ratios of their dimensions (axis).

Each cell in the grid corresponds to a small collection of spaceships with similar shape properties. By clicking a cell, the best spaceship in the cell is displayed in the "Spaceship Preview".

Each spaceship has a "fitness" value assigned, which measures how _good_ the spaceship is according to different metrics. The higher the fitness, the better the spaceship is.
"""

spaceship_preview_help = """
You can click and drag the preview to rotate the spaceship, and use the mouse scrollwheel to zoom in or out.

Hovering over the spaceship blocks preview will show the corresponding block type.
"""

download_help = """
### Colors
You can select a different color for the armor blocks in your spaceship! Simply pick a color from the palette below and press the "Apply" button to update the existing spaceships color.

### Download
You can download the currently selected spaceship as a `.zip` file by clicking the **Download** button. 
The compressed folder, located in your default download directory, contains the files needed to load the spaceship in Space Engineers as a blueprint (`bp.sbc` and `thumb.png`), as well as a program-specific file used in the "Spaceship Comparator" application.

Simply place the unzipped folder in `..\AppData\Roaming\SpaceEngineers\Blueprints\local` and load Space Engineers.
In a scenario world, press `Ctrl+F10` to bring up the **Blueprints** window and you will see the spaceship listed among the local blueprints.
"""

user_study_quit_msg = """
Do you really want to quit the user study?

If you quit the user study, we won't collect data anymore and you won't be able to complete the experiment.
Make sure to close the questionnaire afterwards.
"""

toggle_safe_rules_off_msg = """
Do you really want to turn off safe mode? This will reset the current progress of evolution, because the population of spaceships will need to be re-initialized.

By toggling off this feature, we will not check that thrusters are placed on all six sides of the spaceship, making it difficult to maneuver without manually editing the ship in Space Engineers.

On the other hand, the evolution will be able to propose a more diverse population of spaceships, allowing you to explore a richer set of hull shapes.
"""

toggle_safe_rules_on_msg = """
Do you want to turn on safe mode again? This will reset the current progress of evolution, because the population of spaceships will need to be re-initialized.
"""