with Ada.Command_Line;
with Ada.Directories;
with Ada.Exceptions;
with Ada.Streams;
with Ada.Streams.Stream_IO;
with Ada.Strings;
with Ada.Strings.Fixed;
with Ada.Text_IO;
with Ada.Unchecked_Deallocation;
with Indexed_Records;
with Interfaces;

procedure Records_CLI is
   package IR renames Indexed_Records;
   package IO renames Ada.Streams.Stream_IO;
   use type Ada.Streams.Stream_Element_Offset;
   use type IR.U64;

   subtype Bytes is Ada.Streams.Stream_Element_Array;
   subtype Offset is Ada.Streams.Stream_Element_Offset;
   subtype U64 is IR.U64;
   type Byte_Access is access Bytes;
   procedure Free is new Ada.Unchecked_Deallocation (Bytes, Byte_Access);

   Max_Record : constant := 2_097_152;
   Input, Output : IO.File_Type;
   W : IR.Writer;
   R : IR.Reader;
   Payload : Byte_Access := null;

   procedure Cleanup is
   begin
      begin
         if IO.Is_Open (Input) then IO.Close (Input); end if;
      exception
         when others => null;
      end;
      begin
         if IO.Is_Open (Output) then IO.Close (Output); end if;
      exception
         when others => null;
      end;
      begin
         IR.Abort_Write (W);
      exception
         when others => null;
      end;
      begin
         IR.Close (R);
      exception
         when others => null;
      end;
      Free (Payload);
   end Cleanup;

   procedure Usage is
   begin
      raise IR.Format_Error with
        "usage: records_cli pack MODE INPUT OUTPUT | unpack ARCHIVE OUTPUT | " &
        "get ARCHIVE ORDINAL OUTPUT | verify ARCHIVE | info ARCHIVE";
   end Usage;

   procedure Distinct_Paths (Source, Destination : String) is
   begin
      if Ada.Directories.Exists (Destination) then
         raise IR.Format_Error with "output already exists";
      end if;
      if Ada.Directories.Full_Name (Source) =
        Ada.Directories.Full_Name (Destination)
      then
         raise IR.Format_Error with "input and output must be different files";
      end if;
   end Distinct_Paths;

   procedure Read_Exact (Data : out Bytes; Error_Message : String) is
      Next : Offset := Data'First;
      Last : Offset;
   begin
      while Next <= Data'Last loop
         if IO.End_Of_File (Input) then
            raise IR.Format_Error with Error_Message;
         end if;
         IO.Read (Input, Data (Next .. Data'Last), Last);
         if Last < Next then
            raise IR.Format_Error with Error_Message;
         end if;
         Next := Last + 1;
      end loop;
   end Read_Exact;

   function Mode_From (Name : String) return IR.Group_Mode is
   begin
      if Name = "grouped" then
         return IR.Grouped;
      elsif Name = "per-record" then
         return IR.Per_Record;
      elsif Name = "monolithic" then
         return IR.Monolithic;
      end if;
      raise IR.Format_Error with "mode must be grouped, per-record or monolithic";
   end Mode_From;

   function Ordinal_From (Text : String) return U64 is
      Value : U64 := 0;
      Digit : U64;
   begin
      if Text'Length = 0 then
         raise IR.Format_Error with "ordinal must be an unsigned decimal integer";
      end if;
      for Ch of Text loop
         if Ch not in '0' .. '9' then
            raise IR.Format_Error with "ordinal must be an unsigned decimal integer";
         end if;
         Digit := U64 (Character'Pos (Ch) - Character'Pos ('0'));
         if Value > (U64'Last - Digit) / 10 then
            raise IR.Format_Error with "ordinal is too large";
         end if;
         Value := Value * 10 + Digit;
      end loop;
      return Value;
   end Ordinal_From;

   function Number (Value : U64) return String is
     (Ada.Strings.Fixed.Trim (U64'Image (Value), Ada.Strings.Both));

   procedure Pack (Mode : IR.Group_Mode; Source, Destination : String) is
      Header : Bytes (1 .. 8);
      Length : U64;
   begin
      Distinct_Paths (Source, Destination);
      IO.Open (Input, IO.In_File, Source);
      IR.Create (W, Destination, Block_Target => 262_144,
                 Max_Record_Bytes => Max_Record, Mode => Mode);
      while not IO.End_Of_File (Input) loop
         Read_Exact (Header, "truncated record length");
         Length := 0;
         for J in Header'Range loop
            Length := Length or
              Interfaces.Shift_Left (U64 (Header (J)), Natural (J - 1) * 8);
         end loop;
         if Length > U64 (Max_Record) then
            raise IR.Format_Error with "record exceeds the 2097152-byte limit";
         end if;
         Payload := new Bytes (1 .. Offset (Length));
         Read_Exact (Payload.all, "truncated record payload");
         IR.Append (W, Payload.all);
         Free (Payload);
      end loop;
      IO.Close (Input);
      IR.Close (W);
   end Pack;

   procedure Show_Info is
      N : constant Natural := IR.Frame_Count (R);
      Metadata : constant U64 := IR.Index_Bytes (R);
      Seek_Table_Bytes : constant U64 := 17 + 8 * (U64 (N) + 1);
   begin
      if Metadata < Seek_Table_Bytes then
         raise IR.Format_Error with "invalid index byte accounting";
      end if;
      Ada.Text_IO.Put
        ("{""count"":" & Number (IR.Count (R)) &
         ",""data_frames"":" & Number (U64 (N)) &
         ",""archive_bytes"":" & Number (IR.Archive_Bytes (R)) &
         ",""index_bytes"":" & Number (Metadata) &
         ",""compressed_sizes"":[");
      for J in 1 .. N loop
         if J > 1 then Ada.Text_IO.Put (","); end if;
         Ada.Text_IO.Put (Number (IR.Compressed_Size (R, J)));
      end loop;
      if N > 0 then Ada.Text_IO.Put (","); end if;
      Ada.Text_IO.Put
        (Number (Metadata - Seek_Table_Bytes) & "],""decompressed_sizes"":[");
      for J in 1 .. N loop
         if J > 1 then Ada.Text_IO.Put (","); end if;
         Ada.Text_IO.Put (Number (IR.Decoded_Size (R, J)));
      end loop;
      if N > 0 then Ada.Text_IO.Put (","); end if;
      Ada.Text_IO.Put_Line ("0]}");
   end Show_Info;

begin
   if Ada.Command_Line.Argument_Count = 0 then Usage; end if;
   declare
      Command : constant String := Ada.Command_Line.Argument (1);
      Args : constant Natural := Ada.Command_Line.Argument_Count;
   begin
      if Command = "pack" then
         if Args /= 4 then Usage; end if;
         Pack (Mode_From (Ada.Command_Line.Argument (2)),
               Ada.Command_Line.Argument (3), Ada.Command_Line.Argument (4));
      elsif Command = "unpack" then
         if Args /= 3 then Usage; end if;
         Distinct_Paths (Ada.Command_Line.Argument (2), Ada.Command_Line.Argument (3));
         IR.Open (R, Ada.Command_Line.Argument (2));
         IR.Unpack (R, Ada.Command_Line.Argument (3));
         IR.Close (R);
      elsif Command = "get" then
         if Args /= 4 then Usage; end if;
         Distinct_Paths (Ada.Command_Line.Argument (2), Ada.Command_Line.Argument (4));
         declare
            Ordinal : constant U64 := Ordinal_From (Ada.Command_Line.Argument (3));
         begin
            IR.Open (R, Ada.Command_Line.Argument (2));
            Payload := new Bytes'(IR.Read (R, Ordinal));
            IO.Create (Output, IO.Out_File, Ada.Command_Line.Argument (4));
            if Payload'Length > 0 then IO.Write (Output, Payload.all); end if;
            IO.Close (Output);
            Free (Payload);
            IR.Close (R);
         end;
      elsif Command = "verify" or else Command = "info" then
         if Args /= 2 then Usage; end if;
         IR.Open (R, Ada.Command_Line.Argument (2));
         if Command = "verify" then IR.Validate (R); else Show_Info; end if;
         IR.Close (R);
      else
         Usage;
      end if;
   end;
   Cleanup;
exception
   when Error : others =>
      Cleanup;
      Ada.Text_IO.Put_Line
        (Ada.Text_IO.Standard_Error,
         "records_cli: " & Ada.Exceptions.Exception_Message (Error));
      Ada.Command_Line.Set_Exit_Status (Ada.Command_Line.Failure);
end Records_CLI;
